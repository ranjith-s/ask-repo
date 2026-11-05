from shop.pricing import Cart, apply_discount


def checkout(cart: Cart, coupon_percent: int = 0):
    """HTTP handler for POST /checkout."""
    total = cart.total_with_tax()
    return {"total": str(apply_discount(total, coupon_percent))}
