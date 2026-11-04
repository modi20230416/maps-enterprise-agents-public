"""HTTP contracts, input boundaries, recovery and read-only event playback."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from enterprise_agent.web.api import create_app
except ImportError:
    TestClient = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(TestClient is None, "install .[web,web-test] to test HTTP API")
class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "api.sqlite3"
        self.app = create_app(self.path, allow_protocol_fixture=True, start_workers=False)
        self.client = TestClient(self.app)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.client.close)

    def submit(self):
        response = self.client.post("/api/tasks", json={"mode":"protocol_fixture"}, headers={"Idempotency-Key":"http-test"})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["task_id"]

    def test_submit_idempotency_and_events_cursor(self):
        task_id = self.submit()
        self.assertEqual(self.submit(), task_id)
        self.assertEqual(len(self.client.get("/api/tasks").json()), 1)
        events = self.client.get(f"/api/tasks/{task_id}/events").json()
        self.assertEqual(len(events), 1)
        self.assertEqual(self.client.get(f"/api/tasks/{task_id}/events?after={events[0]['cursor']}").json(), [])
        self.assertNotIn("lease_token", self.client.get(f"/api/tasks/{task_id}").json())
        response = self.client.post("/api/tasks",json={"mode":"protocol_fixture","max_attempts":3},headers={"Idempotency-Key":"http-test"})
        self.assertEqual(response.status_code,409)

    def test_fixture_explicitly_disabled_by_default(self):
        with TestClient(create_app(self.path, start_workers=False)) as client:
            self.assertEqual(client.post("/api/tasks",json={"mode":"protocol_fixture"}).status_code,403)

    def test_missing_llm_config_never_silently_uses_fixture(self):
        with patch("enterprise_agent.web.api.llm_ready",return_value=False):
            self.assertEqual(self.client.post("/api/tasks",json={}).status_code,503)
        self.assertEqual(self.app.state.store.list(), [])

    def test_bad_or_secret_input_is_rejected_before_persistence(self):
        data=json.loads((ROOT/"examples/orchestrate_llm.json").read_text(encoding="utf-8"))
        data["event"]["payload"]["api_key"]="never-persist-this"
        for body in ({"api_key":"never-persist-this"},{"input":data}, {"input":{}}, {"max_attempts":99}):
            response=self.client.post("/api/tasks",json=body)
            self.assertEqual(response.status_code,422,response.text)
            self.assertNotIn("never-persist-this",response.text)
        self.assertEqual(self.app.state.store.list(), [])

    def test_origin_size_content_type_and_unknown_task(self):
        self.assertEqual(self.client.post("/api/tasks",json={},headers={"Origin":"https://untrusted.example"}).status_code,403)
        self.assertEqual(self.client.post("/api/tasks",content='{}').status_code,415)
        self.assertEqual(self.client.post("/api/tasks",content='x'*300001,headers={"Content-Type":"application/json"}).status_code,413)
        self.assertEqual(self.client.get("/api/tasks/missing").status_code,404)
        cors=self.client.options("/api/tasks",headers={"Origin":"http://127.0.0.1:5173","Access-Control-Request-Method":"POST"})
        self.assertEqual(cors.headers["access-control-allow-origin"],"http://127.0.0.1:5173")
        # Same-origin Swagger/API clients must also be allowed to submit.
        same_origin=self.client.post("/api/tasks",json={"mode":"protocol_fixture"},headers={"Origin":"http://testserver"})
        self.assertEqual(same_origin.status_code,202,same_origin.text)

    def test_http_execution_review_restart_replay_and_rerun(self):
        app=create_app(self.path,allow_protocol_fixture=True)
        with TestClient(app) as client:
            task_id=client.post("/api/tasks",json={"mode":"protocol_fixture"}).json()["task_id"]
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                task=client.get(f"/api/tasks/{task_id}").json()
                if task["status"]=="WAITING_REVIEW": break
                time.sleep(.05)
            self.assertEqual(task["status"],"WAITING_REVIEW",task)
            for review in task["reviews"]:
                response=client.post(f"/api/tasks/{task_id}/reviews/{review['module']}",json={"binding":review["binding"],"decision":"APPROVED","reviewer":"演示复核人"})
                self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()["status"],"READY")
        with TestClient(create_app(self.path,allow_protocol_fixture=True,start_workers=False)) as client:
            with patch("enterprise_agent.infrastructure.llm.LLMClient.complete",side_effect=AssertionError("replay must not invoke model")):
                replay=client.get(f"/api/tasks/{task_id}/replay").json()
                self.assertEqual(replay["status"],"READY")
                self.assertFalse(replay["execution_authorized"])
                self.assertEqual(len(replay["reviews"]),4)
            with client.stream("GET",f"/api/tasks/{task_id}/events/stream",headers={"Last-Event-ID":"1"}) as stream:
                data=stream.read().decode()
                self.assertIn("event: task-event",data)
                self.assertNotIn('"cursor": 1,',data)
            rerun=client.post(f"/api/tasks/{task_id}/rerun").json()
            self.assertNotEqual(rerun["task_id"],task_id)
            self.assertEqual(client.get(f"/api/tasks/{rerun['task_id']}").json()["parent_id"],task_id)
