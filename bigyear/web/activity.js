"use strict";

export function buildActivity(route) {
  let total = {miles: 0, birdingHours: 0, restDays: 0};
  return route.days.map(day => {
    const before = total;
    total = {
      miles: before.miles + day.road_miles + (day.boat_miles ?? 0),
      birdingHours: before.birdingHours + day.stops.reduce((n,stop) => n + (stop.birding_minutes ?? route.config.birding_minutes), 0) / 60,
      restDays: before.restDays + Number(day.kind === 'rest'),
    };
    return {date: day.date, kind: day.kind, before, ...total};
  });
}

export function activityAt(days, index, playback) {
  const day = days[index];
  return {
    miles: day.before.miles + playback.miles,
    birdingHours: day.before.birdingHours + playback.birdingMinutes / 60,
    // Rest days are whole completed calendar days, never fractions of a pause.
    restDays: day.before.restDays + Number(day.kind === 'rest' && playback.progress === 1),
  };
}

export function activityCurve(days, key, xy, count = days.length) {
  const points = [xy(0, 0)];
  days.slice(0, count).forEach((day, i) => {
    if (key === 'restDays') points.push(xy(i + 1, day.before[key]));
    points.push(xy(i + 1, day[key]));
  });
  return points.map((point, i) => (i ? 'L' : 'M') + point).join(' ');
}
