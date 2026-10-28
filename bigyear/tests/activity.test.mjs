import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';

const load = async file => import(`data:text/javascript;base64,${readFileSync(new URL(file, import.meta.url)).toString('base64')}`);
const {buildActivity, activityAt, activityCurve} = await load('../web/activity.js');
const {buildPlayback, playbackState} = await load('../web/playback.js');
const locations = [{lon: 0, lat: 0}, {lon: 1, lat: 0}];
const stop = location => ({location, ...locations[location], name: 'Birding site', new_species: []});
const leg = {from: 0, to: 1, mode: 'road', minutes: 60, miles: 30, start_fraction: 0, end_fraction: 1};
const route = {
  initial_location: 0, locations, config: {birding_minutes: 120},
  days: [
    {date: '2024-02-28', kind: 'birding', stops: [stop(0), stop(1)], legs: [leg], road_miles: 30, activity_minutes: 300},
    {date: '2024-02-29', kind: 'rest', stops: [], legs: [], road_miles: 0, activity_minutes: 0},
    {date: '2024-03-01', kind: 'road_transit', stops: [], legs: [{...leg, from: 1, to: 0}], road_miles: 30, activity_minutes: 60},
    {date: '2024-03-02', kind: 'rest', stops: [], legs: [], road_miles: 0, activity_minutes: 0},
  ],
};

test('activity counts all stops, includes transit miles, and distinguishes rest from non-birding travel', () => {
  const days = buildActivity(route);
  assert.deepEqual(days.map(({miles, birdingHours, restDays}) => [miles, birdingHours, restDays]), [
    [30, 4, 0], [30, 4, 1], [60, 4, 1], [60, 4, 2],
  ]);
  assert.deepEqual(days[0].before, {miles: 0, birdingHours: 0, restDays: 0});
});

test('birding time accumulates only during stops, never driving or resting', () => {
  const days = buildActivity(route), timelines = buildPlayback(route);
  const [first, drive, second] = timelines[0].events;
  const duringBirding = playbackState(timelines[0], first.duration / 2);
  assert.equal(duringBirding.birdingMinutes, 60);
  assert.deepEqual(activityAt(days, 0, duringBirding), {miles: 0, birdingHours: 1, restDays: 0});
  const duringDrive = playbackState(timelines[0], drive.start + drive.duration / 2);
  assert.equal(duringDrive.minutes, 150);
  assert.equal(duringDrive.birdingMinutes, 120);
  assert.deepEqual(activityAt(days, 0, duringDrive), {miles: 15, birdingHours: 2, restDays: 0});
  assert.deepEqual(activityAt(days, 0, playbackState(timelines[0], second.end)), {miles: 30, birdingHours: 4, restDays: 0});
  assert.deepEqual(activityAt(days, 2, playbackState(timelines[2], timelines[2].duration / 2)), {miles: 45, birdingHours: 4, restDays: 1});
});

test('rest credit waits until the day ends and seeking backward does not retain future totals', () => {
  const days = buildActivity(route), timelines = buildPlayback(route);
  assert.deepEqual(activityAt(days, 1, playbackState(timelines[1], 0)), {miles: 30, birdingHours: 4, restDays: 0});
  assert.equal(activityAt(days, 1, playbackState(timelines[1], timelines[1].duration / 2)).restDays, 0);
  assert.equal(activityAt(days, 1, playbackState(timelines[1], timelines[1].duration)).restDays, 1);
  assert.equal(activityAt(days, 3, playbackState(timelines[3], timelines[3].duration)).restDays, 2);
  assert.equal(activityAt(days, 0, playbackState(timelines[0], 0)).restDays, 0);
  assert.equal(activityCurve(days, 'restDays', (x, y) => [x, y]), 'M0,0 L1,0 L1,0 L2,0 L2,1 L3,1 L3,1 L4,1 L4,2');
  assert.equal(activityCurve(days, 'miles', (x, y) => [x, y], 2), 'M0,0 L1,30 L2,30');
});

test('all-rest and shorter routes have defined zero totals and use their configured stop allowance', () => {
  const rest = buildActivity({...route, days: [route.days[1]]});
  assert.deepEqual(rest[0].before, {miles: 0, birdingHours: 0, restDays: 0});
  assert.equal(rest[0].restDays, 1);
  assert.equal(rest[0].miles, 0);
  const short = buildActivity({...route, config: {birding_minutes: 90}, days: [route.days[0]]});
  assert.equal(short[0].birdingHours, 3);
});

test('onsite birding hours exclude separately charged access walking', () => {
  const saved = {...route, days: [{...route.days[0], stops: [{...stop(0), walking_minutes: 60,
    birding_minutes: 120}], legs: [], road_miles: 0, activity_minutes: 180}]};
  const days = buildActivity(saved), [timeline] = buildPlayback(saved);
  assert.equal(days[0].birdingHours, 2);
  assert.equal(activityAt(days, 0, playbackState(timeline, timeline.duration)).birdingHours, 2);
});

test('saved route activity agrees with every daily activity total, separating walking', {skip: !process.env.BIGYEAR_PILOT_ROUTE}, () => {
  const saved = JSON.parse(readFileSync(process.env.BIGYEAR_PILOT_ROUTE, 'utf8'));
  const days = buildActivity(saved), timelines = buildPlayback(saved);
  let miles = 0, birdingHours = 0, restDays = 0;
  saved.days.forEach((day, i) => {
    miles += day.road_miles + (day.boat_miles ?? 0);
    birdingHours += (day.activity_minutes - day.travel_minutes - (day.walking_minutes ?? 0) - (day.boarding_minutes ?? 0)) / 60;
    restDays += Number(day.kind === 'rest');
    const actual = activityAt(days, i, playbackState(timelines[i], timelines[i].duration));
    assert.ok(Math.abs(actual.miles - miles) < 1e-8);
    assert.ok(Math.abs(actual.birdingHours - birdingHours) < 1e-8);
    assert.equal(actual.restDays, restDays);
  });
  if(saved.search.road_miles != null) assert.ok(Math.abs(days.at(-1).miles - saved.search.road_miles - saved.days.reduce((n,d)=>n+(d.boat_miles??0),0)) < 1e-7);
  assert.ok(Math.abs(days.at(-1).birdingHours - birdingHours) < 1e-8);
  assert.equal(days.at(-1).restDays, restDays);
});
