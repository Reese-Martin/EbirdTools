import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';

// Load the browser ES module without changing the Python project's Node settings.
const source = readFileSync(new URL('../web/playback.js', import.meta.url));
const {geometry, positionAt, portion, buildPlayback, playbackState, travelSummary} = await import(`data:text/javascript;base64,${source.toString('base64')}`);
const locations = [{lon: 0, lat: 0}, {lon: 3, lat: 0}, {lon: 3, lat: 4}];
const stop = (location, species) => ({location, ...locations[location], name: `Site ${location}`, new_species: species});
const leg = (from, to, options = {}) => ({from, to, mode: 'road', minutes: 20, miles: 10, start_fraction: 0, end_fraction: 1, ...options});
const route = days => ({initial_location: 0, locations, config: {birding_minutes: 120}, days});

test('trace follows polyline distance through bends and duplicate vertices', () => {
  const g = geometry([[0, 0], [0, 0], [3, 0], [3, 4]]);
  assert.deepEqual(positionAt(g, 0.5), [3, 0.5]);
  assert.deepEqual(portion(g, 2 / 7, 5 / 7), [[2, 0], [3, 0], [3, 2]]);
  assert.deepEqual(positionAt(geometry([[2, 2], [2, 2]]), 0.4), [2, 2]);
});

test('multiple stops interleave with travel; species credit waits for the visit', () => {
  const [day] = buildPlayback(route([{stops: [stop(0, ['a']), stop(1, ['b']), stop(2, ['c'])], legs: [leg(0, 1), leg(1, 2)]}]));
  assert.deepEqual(day.events.map(e => e.kind), ['stop', 'travel', 'stop', 'travel', 'stop']);
  const drive = day.events[1];
  const midway = playbackState(day, drive.start + drive.duration / 2);
  assert.deepEqual(midway.cursor, [1.5, 0]);
  assert.equal(midway.miles, 5);
  assert.equal(midway.minutes, 130);
  assert.equal(midway.newSpecies, 1);
  assert.deepEqual(midway.stops, [0]);
  assert.equal(midway.status, 'Driving');
  const arrival = playbackState(day, drive.end);
  assert.deepEqual(arrival.stops, [0, 1]);
  assert.equal(arrival.newSpecies, 1);
  const done = playbackState(day, day.duration);
  assert.equal(done.newSpecies, 3);
  assert.equal(done.miles, 20);
  assert.equal(done.minutes, 400);
  assert.equal(done.status, 'Day complete');
});

test('multi-day transit portions meet without retracing the whole leg; rest holds position', () => {
  const timelines = buildPlayback(route([
    {stops: [], legs: [leg(0, 1, {end_fraction: 0.5})]},
    {stops: [], legs: []},
    {stops: [], legs: [leg(0, 1, {start_fraction: 0.5})]},
    {stops: [stop(1, ['a'])], legs: []},
  ]));
  assert.deepEqual(timelines[0].end, [1.5, 0]);
  assert.deepEqual(timelines[1].start, [1.5, 0]);
  assert.deepEqual(playbackState(timelines[1], 150).cursor, [1.5, 0]);
  assert.deepEqual(playbackState(timelines[2], timelines[2].duration / 2).cursor, [2.25, 0]);
  assert.deepEqual(timelines[2].end, [3, 0]);
  assert.equal(playbackState(timelines[2], timelines[2].duration).newSpecies, 0);
});

test('distinct pins sharing a road anchor retain separate visits and credits', () => {
  const [day] = buildPlayback(route([{stops: [stop(0, ['a']), stop(1, ['b'])], legs: [leg(0, 1, {minutes: 0, miles: 0, geometry: {coordinates: [[1, 1], [1, 1]]}})]}]));
  const drive = day.events[1];
  assert.deepEqual(playbackState(day, drive.start + 10).cursor, [1, 1]);
  const done = playbackState(day, day.duration);
  assert.equal(done.minutes, 240);
  assert.equal(done.miles, 0);
  assert.equal(done.newSpecies, 2);
});

test('flight marker follows its schematic arch and has no species or road miles', () => {
  const [day] = buildPlayback(route([{stops: [], legs: [leg(0, 1, {mode: 'flight', miles: 0})]}]));
  const state = playbackState(day, day.duration / 2);
  assert.ok(state.cursor[1] > 0);
  assert.equal(state.newSpecies, 0);
  assert.equal(state.miles, 0);
  assert.equal(state.status, 'In flight');
});

test('driving playback follows the road bends and is identified as driving only', () => {
  const saved = route([{stops: [stop(2, ['a'])], legs: [leg(0, 2, {
    geometry: {type: 'LineString', coordinates: [[0, 0], [3, 0], [3, 4]]}, schematic: false,
  })]}]);
  const summary = travelSummary(saved);
  assert.equal(summary.label, 'Driving only · road network paths');
  assert.equal(summary.hasFlights, false);
  assert.equal(summary.hasSchematicRoads, false);
  const [day] = buildPlayback(saved), drive = day.events[0];
  const midway = playbackState(day, drive.duration / 2);
  assert.deepEqual(midway.cursor, [3, .5]);
  assert.equal(midway.status, 'Driving');
  const missingShape = travelSummary(route([{stops: [], legs: [leg(0, 1)]}]));
  assert.equal(missingShape.hasFlights, false);
  assert.equal(missingShape.hasSchematicRoads, true);
  assert.ok(missingShape.label.startsWith('Driving only'));
  assert.equal(travelSummary(route([{stops: [], legs: [leg(0, 1, {mode: 'flight'})]}])).hasFlights, true);
});

test('all real pilot day totals and endpoints agree with the saved model', {skip: !process.env.BIGYEAR_PILOT_ROUTE}, () => {
  const saved = JSON.parse(readFileSync(process.env.BIGYEAR_PILOT_ROUTE, 'utf8'));
  buildPlayback(saved).forEach((timeline, i) => {
    const state = playbackState(timeline, timeline.duration), day = saved.days[i];
    assert.equal(state.newSpecies, day.new_species.length);
    assert.ok(Math.abs(state.miles - day.road_miles - (day.boat_miles ?? 0)) < 1e-8);
    assert.ok(Math.abs(state.minutes - day.activity_minutes) < 1e-8);
    assert.ok(state.cursor.every(Number.isFinite));
  });
});

test('charter playback returns to port and includes boarding and dock walking', () => {
  const saved=route([{kind:'pelagic',road_miles:0,boat_miles:20,boarding_minutes:60,walking_minutes:30,
    activity_minutes:250,stops:[stop(1,['bird'])],
    legs:[leg(0,1,{mode:'boat'}),leg(1,0,{mode:'boat'})]}]);
  const [day]=buildPlayback(saved);
  assert.deepEqual(day.events.map(e=>e.kind),['access','travel','stop','travel','access']);
  const sailing=day.events[1];
  assert.equal(playbackState(day,sailing.start+sailing.duration/2).status,'Aboard modeled charter');
  const state=playbackState(day,day.duration);
  assert.deepEqual(state.cursor,[0,0]);
  assert.equal(state.minutes,250);
  assert.equal(state.birdingMinutes,120);
  assert.equal(state.walkingMinutes,30);
  assert.equal(state.miles,20);
  assert.equal(travelSummary(saved).hasBoats,true);
  assert.ok(travelSummary(saved).label.includes('charters'));
});

test('car-profile ferry steps are labeled explicitly and keep frozen travel totals', () => {
  const saved = route([{stops: [stop(1, ['a'])], legs: [leg(0, 1, {
    geometry: {type: 'LineString', coordinates: [[0, 0], [1, 0]]},
    vehicle_ferry_segments: [{name: 'Vehicle ferry', minutes: 35, miles: 4.3}],
  })]}]);
  const summary = travelSummary(saved);
  assert.equal(summary.hasVehicleFerries, true);
  assert.equal(summary.hasFlights, false);
  assert.equal(summary.label, 'Driving + vehicle ferries · road network paths');
  const [day] = buildPlayback(saved);
  assert.equal(playbackState(day, day.events[0].duration / 2).status, 'Driving / vehicle ferry');
  assert.equal(playbackState(day, day.duration).miles, saved.days[0].legs[0].miles);
});

test('explicit round-trip walking consumes activity time without inflating birding hours', () => {
  const saved = route([{stops: [{...stop(0, ['a']), walking_minutes: 60, birding_minutes: 120}],
    legs: [], activity_minutes: 180, road_miles: 0}]);
  const [day] = buildPlayback(saved), state = playbackState(day, day.duration);
  assert.equal(travelSummary(saved).label, 'Walking access');
  assert.equal(travelSummary(saved).hasWalking, true);
  assert.equal(state.minutes, 180);
  assert.equal(state.birdingMinutes, 120);
  assert.equal(state.walkingMinutes, 60);
  const halfway = playbackState(day, day.duration / 2);
  assert.equal(halfway.minutes, 90);
  assert.equal(halfway.walkingMinutes, 30);
  assert.equal(halfway.newSpecies, 0);
  assert.ok(halfway.status.startsWith('Walking + birding'));
});

test('joint charter arrives by car before boarding and credits only the offshore stop', () => {
  const saved = route([{kind:'pelagic',boarding_minutes:60,walking_minutes:20,stops:[stop(2,['petrel'])],
    legs:[leg(0,1),leg(1,2,{mode:'boat',minutes:40,miles:15}),leg(2,1,{mode:'boat',minutes:40,miles:15})]}]);
  const [timeline]=buildPlayback(saved);
  assert.deepEqual(timeline.events.map(e=>e.kind),['travel','access','travel','stop','travel','access']);
  assert.deepEqual(playbackState(timeline,timeline.events[2].start+1).newSpecies,0);
  const done=playbackState(timeline,timeline.duration);
  assert.deepEqual(done.cursor,[3,0]);
  assert.equal(done.newSpecies,1);
  assert.equal(done.miles,40);
  assert.equal(done.minutes,300);
});

test('island playback separates crossings, full walking and terrestrial birding', () => {
  const walk={type:'LineString',coordinates:[[2.9,0],[3,0]]};
  const saved=route([{kind:'island',boarding_minutes:60,walking_minutes:60,
    stops:[{...stop(1,['jay']),walking_minutes:40,birding_minutes:120,landing:'prisoners'}],
    legs:[leg(0,1,{mode:'boat',minutes:120,miles:25,modeled_island_transfer:true,
      geometry:{type:'LineString',coordinates:[[0,0],[2.9,0]]},island_walk_geometry:walk}),
      leg(1,0,{mode:'boat',minutes:120,miles:25,modeled_island_transfer:true,
      geometry:{type:'LineString',coordinates:[[2.9,0],[0,0]]},island_walk_geometry:walk})]}]);
  const [timeline]=buildPlayback(saved),done=playbackState(timeline,timeline.duration);
  assert.deepEqual(timeline.events.map(e=>e.kind),['access','travel','travel','stop','travel','travel','access']);
  assert.equal(timeline.events[2].leg.mode,'walk');
  assert.deepEqual(timeline.events[2].shape.coords,walk.coordinates);
  assert.equal(done.minutes,480);
  assert.equal(done.birdingMinutes,120);
  assert.equal(done.walkingMinutes,60);
  assert.equal(done.miles,50);
  assert.equal(done.newSpecies,1);
  assert.deepEqual(done.cursor,[0,0]);
  assert.ok(travelSummary(saved).label.includes('modeled boat trips'));
});
