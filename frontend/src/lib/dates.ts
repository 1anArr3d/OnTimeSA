// `Date.toISOString()` always renders in UTC, which silently shifts "today"
// forward a day for anyone west of UTC in the evening (e.g. Central time is
// UTC-5/-6, so anytime after ~6-7pm local is already tomorrow in UTC). Format
// from local date components instead so "today" matches the user's wall clock.
function toLocalIsoDate(d: Date): string {
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function todayIso(): string {
  return toLocalIsoDate(new Date());
}

export function isoDateDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return toLocalIsoDate(d);
}
