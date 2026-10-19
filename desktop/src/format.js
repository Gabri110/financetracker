// Currency formatting shared by every page. An unknown currency code (possible
// from imported files) falls back to a plain number instead of throwing and
// blanking the whole page.
export function fmt(n, currency = "USD") {
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(n);
  } catch {
    return `${Number(n || 0).toFixed(2)} ${currency}`;
  }
}
