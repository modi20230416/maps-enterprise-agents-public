"""Admission checks of current facts, never inventory or procurement forecasts."""

def admission_errors(context):
    errors = [f"production requirements not confirmed: {w.wo_id}"
              for w in context.work_orders if not w.production_ready]
    needed = {}
    for order in context.snapshot.orders:
        for material, quantity in order.materials.items():
            needed[material] = needed.get(material, 0) + quantity
    for material, quantity in needed.items():
        if quantity > context.snapshot.stock.get(material, 0):
            errors.append(f"current confirmed stock insufficient: {material}")
    if needed and context.snapshot.preparation_minutes:
        errors.append("kits not ready: preparation lead time remains")
    return errors
