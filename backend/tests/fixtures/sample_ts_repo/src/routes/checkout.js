const express = require('express');
const { Cart, applyDiscount } = require('../cart');

const router = express.Router();

/** POST /checkout: price the cart and apply an optional coupon. */
router.post('/checkout', (req, res) => {
  const cart = new Cart();
  req.body.items.forEach((item) => cart.addItem(item));
  res.json({ total: applyDiscount(cart.totalWithTax(), req.body.couponPercent || 0) });
});

module.exports = router;
