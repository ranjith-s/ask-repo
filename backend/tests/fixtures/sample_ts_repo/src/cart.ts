export const TAX_RATE = 0.18;

/** A line in the shopping cart. */
export interface CartItem {
  name: string;
  price: number;
}

export class Cart {
  private items: CartItem[] = [];

  addItem(item: CartItem): void {
    this.items.push(item);
  }

  /** Total price including tax. */
  totalWithTax(): number {
    const subtotal = this.items.reduce((sum, item) => sum + item.price, 0);
    return subtotal * (1 + TAX_RATE);
  }
}

export function applyDiscount(price: number, percent: number): number {
  if (percent < 0 || percent > 100) throw new RangeError('percent must be between 0 and 100');
  return (price * (100 - percent)) / 100;
}
