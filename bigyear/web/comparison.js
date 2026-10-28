"use strict";

// Reports have calendar dates, not reliable observation times. Compare against
// the complete selected date, even while a route visit is being illustrated.
export function speciesComparison(routeTotal, coverage, date) {
  const day = coverage?.days.find(day => day.date === date);
  if (!day) return null;
  const observed = day.cumulative_species;
  return {observed, gap: observed - routeTotal,
    percentage: observed > 0 ? routeTotal / observed * 100 : null};
}

// Daily step changes avoid implying observations between dates. The selected
// day has its full reported total throughout that day's illustrative playback.
export function observerCurve(days, coverage, xy) {
  const counts = new Map(coverage.days.map(day => [day.date, day.cumulative_species]));
  const previous = coverage.days.filter(day => day.date < days[0].date).at(-1);
  const points = [xy(0, previous?.cumulative_species ?? 0)];
  days.forEach((day, i) => {
    const total = counts.get(day.date);
    points.push(xy(i, total), xy(i + 1, total));
  });
  return points.map((point, i) => (i ? 'L' : 'M') + point).join(' ');
}
