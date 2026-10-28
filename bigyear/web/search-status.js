// Keep the saved, audited map distinct from an unfinished search candidate.
export function searchNotice(status, route, runPath) {
  if (!route || !['search', 'validation', 'completed', 'failed', 'cancelled'].includes(status?.stage)) return null;
  const states = [...new Set(route.days.flatMap(day => day.stops.map(stop => route.locations[stop.location]?.state)).filter(Boolean))].sort().map(state => state.replace(/^US-/, ''));
  const shown = `Showing saved itinerary: ${route.species_total} species${states.length ? `; birding stops in ${states.join(', ')}` : ''}.`;
  const score = Number.isInteger(status.best_species) ? `${status.best_species} species` : 'score pending';
  let heading, detail, bestPath = null;
  if (status.stage === 'completed') {
    heading = 'National search complete';
    detail = `${shown} Best independently verified result: ${score}.`;
    if (typeof status.best_run === 'string' && /^[A-Za-z0-9_-]+$/.test(status.best_run)) {
      const path = `${status.best_run}/route.json`;
      if (path !== runPath) bestPath = path;
    }
  } else if (status.stage === 'search' || status.stage === 'validation') {
    heading = status.stage === 'search' ? 'National search in progress' : 'National candidate being validated';
    detail = status.verified_best
      ? `${shown} Best independently verified result so far: ${score}. The comparison is still running.`
      : `${shown} Current candidate: ${score}; final validation pending. The map shows the saved result.`;
  } else {
    heading = status.stage === 'failed' ? 'National search needs attention' : 'National search stopped';
    detail = `${shown} See the run report for status.`;
  }
  return {heading: status.label || heading, detail, bestPath};
}
