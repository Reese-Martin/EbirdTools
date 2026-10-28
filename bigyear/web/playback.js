// Playback is an illustration of the saved itinerary, never an observation clock.
export function travelSummary(route) {
  const legs = route.days.flatMap(day => day.legs);
  const hasFlights = legs.some(leg => leg.mode === 'flight');
  const hasRoads = legs.some(leg => leg.mode === 'road');
  const hasBoats = legs.some(leg => leg.mode === 'boat');
  const hasVehicleFerries = legs.some(leg => leg.vehicle_ferry_segments?.length);
  const hasWalking = route.days.some(day => day.stops.some(stop => (stop.walking_minutes ?? 0) > 0));
  const hasSchematicRoads = legs.some(leg => leg.mode === 'road' && !leg.geometry?.coordinates?.length);
  let label = hasFlights ? (hasRoads ? 'Driving + flights' : 'Flights')
    : hasRoads ? `${hasVehicleFerries ? 'Driving + vehicle ferries' : 'Driving only'} · ${hasSchematicRoads ? 'road shapes unavailable' : 'road network paths'}`
    : 'No travel scheduled';
  if (hasWalking) label = hasRoads || hasFlights
    ? (label.includes(' · ') ? label.replace('Driving only', 'Driving').replace(' · ', ' + walking · ') : label + ' + walking')
    : 'Walking access';
  const boatLabel = route.days.some(day => day.kind === 'island') ? 'modeled boat trips' : 'modeled charters';
  if (hasBoats) label = hasRoads || hasFlights || hasWalking ? label + ' + ' + boatLabel : boatLabel[0].toUpperCase() + boatLabel.slice(1);
  return {hasFlights, hasRoads, hasBoats, hasWalking, hasVehicleFerries, hasSchematicRoads, label};
}

export function geometry(coords) {
  const lengths = [0];
  for (let i = 1; i < coords.length; i++) {
    lengths.push(lengths.at(-1) + Math.hypot(coords[i][0] - coords[i - 1][0], coords[i][1] - coords[i - 1][1]));
  }
  return {coords, lengths, total: lengths.at(-1)};
}

export function positionAt(g, fraction) {
  if (fraction >= 1) return g.coords.at(-1);
  if (fraction <= 0 || !g.total) return g.coords[0];
  const target = g.total * fraction;
  let lo = 1, hi = g.lengths.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (g.lengths[mid] < target) lo = mid + 1;
    else hi = mid;
  }
  const t = (target - g.lengths[lo - 1]) / (g.lengths[lo] - g.lengths[lo - 1] || 1);
  return g.coords[lo - 1].map((v, j) => v + (g.coords[lo][j] - v) * t);
}

export function portion(g, start = 0, end = 1) {
  const a = Math.max(0, Math.min(1, start)), b = Math.max(a, Math.min(1, end));
  if (a === 0 && b === 1) return g.coords;
  return [positionAt(g, a), ...g.coords.filter((_, i) => g.lengths[i] > a * g.total && g.lengths[i] < b * g.total), positionAt(g, b)];
}

function legGeometry(leg, locations) {
  const a = locations[leg.from], b = locations[leg.to];
  let coords = leg.geometry?.coordinates;
  if (!coords?.length) coords = [[a.lon, a.lat], [b.lon, b.lat]];
  if (leg.mode === 'flight') {
    const control = [(a.lon + b.lon) / 2, (a.lat + b.lat) / 2 + 65 / 22];
    coords = Array.from({length: 65}, (_, i) => {
      const t = i / 64, u = 1 - t;
      return [u * u * a.lon + 2 * u * t * control[0] + t * t * b.lon, u * u * a.lat + 2 * u * t * control[1] + t * t * b.lat];
    });
  }
  return geometry(portion(geometry(coords), leg.start_fraction ?? 0, leg.end_fraction ?? 1));
}

export function buildPlayback(route) {
  let here = route.initial_location;
  let cursor = [route.locations[here].lon, route.locations[here].lat];
  return route.days.map(day => {
    const start = cursor, events = [];
    let elapsed = 0, legIndex = 0;
    const add = event => {
      event.start = elapsed;
      elapsed += event.duration;
      event.end = elapsed;
      events.push(event);
    };
    const travel = leg => {
      const shape = legGeometry(leg, route.locations);
      add({kind: 'travel', leg, shape, duration: Math.max(1200, Math.min(8000, (leg.minutes || 0) * 55))});
      cursor = shape.coords.at(-1);
      here = leg.to;
    };
    const access = () => {
      if (day.kind === 'pelagic' || day.kind === 'island') {
        const dockWalking = (day.walking_minutes ?? 0) - (day.kind === 'island' ? day.stops.reduce((n,s)=>n+(s.walking_minutes??0),0) : 0);
        add({kind:'access', duration:450,
          minutes:((day.boarding_minutes ?? 0)+dockWalking)/2,
          walkingMinutes:dockWalking/2});
      }
    };
    if (day.kind === 'pelagic' || day.kind === 'island') {
      while (legIndex < day.legs.length && day.legs[legIndex].mode === 'road') travel(day.legs[legIndex++]);
    }
    access();
    for (const [stopIndex, stop] of day.stops.entries()) {
      const islandLeg = day.kind === 'island' ? day.legs.find(l=>l.modeled_island_transfer) : null;
      const islandWalk = islandLeg?.island_walk_geometry?.coordinates;
      if (islandWalk) travel(day.legs[legIndex++]);
      else while (here !== stop.location && legIndex < day.legs.length) travel(day.legs[legIndex++]);
      const landing = islandWalk ? here : null;
      if (islandWalk) travel({from:landing,to:stop.location,mode:'walk',minutes:(stop.walking_minutes??0)/2,
        miles:0,geometry:{type:'LineString',coordinates:islandWalk},schematic:true});
      const birdingMinutes = stop.birding_minutes ?? route.config.birding_minutes;
      const walkingMinutes = islandWalk ? 0 : stop.walking_minutes ?? 0;
      add({kind: 'stop', stop, stopIndex, duration: 700, minutes: birdingMinutes + walkingMinutes,
        birdingMinutes, walkingMinutes});
      here = stop.location;
      cursor = [stop.lon, stop.lat];
      if (islandWalk) travel({from:stop.location,to:landing,mode:'walk',minutes:(stop.walking_minutes??0)/2,
        miles:0,geometry:{type:'LineString',coordinates:[...islandWalk].reverse()},schematic:true});
    }
    while (legIndex < day.legs.length) travel(day.legs[legIndex++]);
    access();
    if (!events.length) add({kind: 'rest', duration: 350});
    return {start, end: cursor, events, duration: elapsed};
  });
}

export function playbackState(timeline, elapsed) {
  const time = Math.max(0, Math.min(timeline.duration, elapsed));
  let cursor = timeline.start, miles = 0, minutes = 0, birdingMinutes = 0, walkingMinutes = 0, newSpecies = 0, status = 'Day complete', activeStop = -1;
  const travels = [], stops = [];
  for (const event of timeline.events) {
    if (time < event.start) break;
    const progress = time >= event.end ? 1 : (time - event.start) / event.duration;
    if (event.kind === 'travel') {
      travels.push(progress);
      cursor = positionAt(event.shape, progress);
      activeStop = -1;
      miles += (event.leg.miles || 0) * progress;
      minutes += (event.leg.minutes || 0) * progress;
      if (event.leg.mode === 'walk') walkingMinutes += (event.leg.minutes || 0) * progress;
      if (progress < 1) status = event.leg.mode === 'flight' ? 'In flight' : event.leg.mode === 'walk' ? 'Walking between landing and observation' : event.leg.mode === 'boat' ? (event.leg.modeled_island_transfer ? 'Aboard modeled island transfer' : 'Aboard modeled charter') : event.leg.vehicle_ferry_segments?.length ? 'Driving / vehicle ferry' : 'Driving';
    } else if (event.kind === 'stop') {
      cursor = [event.stop.lon, event.stop.lat];
      stops.push(event.stopIndex);
      minutes += event.minutes * progress;
      birdingMinutes += event.birdingMinutes * progress;
      walkingMinutes += event.walkingMinutes * progress;
      activeStop = event.stopIndex;
      // Reveal the stop on arrival; credit species once its visit is complete.
      if (progress === 1) newSpecies += event.stop.new_species.length;
      else status = `${event.walkingMinutes ? 'Walking + birding' : 'Birding'} · ${event.stop.name}`;
    } else if (event.kind === 'access') {
      minutes += event.minutes * progress;
      walkingMinutes += event.walkingMinutes * progress;
      if (progress < 1) status = 'Boarding / dock walking';
    } else status = progress < 1 ? 'Rest · position retained' : 'Day complete';
    if (progress < 1) break;
  }
  return {cursor, miles, minutes, birdingMinutes, walkingMinutes, newSpecies, status, activeStop, travels, stops, progress: time / timeline.duration};
}
