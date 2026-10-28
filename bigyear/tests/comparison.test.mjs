import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../web/comparison.js', import.meta.url));
const {speciesComparison, observerCurve} = await import(`data:text/javascript;base64,${source.toString('base64')}`);
const coverage = {days: [
  {date: '2024-02-28', cumulative_species: 10},
  {date: '2024-02-29', cumulative_species: 12},
  {date: '2024-03-01', cumulative_species: 12},
]};

test('selected full-day observer count stays fixed while route visits gain species', () => {
  assert.deepEqual(speciesComparison(6, coverage, '2024-02-29'), {observed: 12, gap: 6, percentage: 50});
  assert.deepEqual(speciesComparison(9, coverage, '2024-02-29'), {observed: 12, gap: 3, percentage: 75});
  assert.equal(speciesComparison(6, coverage, '2024-02-28').observed, 10);
  assert.equal(speciesComparison(6, null, '2024-02-29'), null);
  assert.equal(speciesComparison(6, coverage, '2023-02-28'), null);
  assert.equal(speciesComparison(0, {days: [{date: '2024-01-01', cumulative_species: 0}]}, '2024-01-01').percentage, null);
});

test('partial-year route curve retains all observations before its start and uses daily steps', () => {
  const path = observerCurve([{date: '2024-02-29'}, {date: '2024-03-01'}], coverage, (x, y) => [x, y]);
  assert.equal(path, 'M0,10 L0,12 L1,12 L1,12 L2,12');
});

test('real saved route comparison follows its actual calendar year and supported pool', {skip: !process.env.BIGYEAR_PILOT_ROUTE}, () => {
  const saved = JSON.parse(readFileSync(process.env.BIGYEAR_PILOT_ROUTE, 'utf8'));
  const coverage = saved.observer_coverage ?? saved.marine_observer_coverage;
  assert.ok(coverage);
  assert.equal(coverage.year, saved.year);
  assert.equal(coverage.days.length, saved.days.length);
  assert.equal(coverage.days[0].date, `${saved.year}-01-01`);
  assert.equal(coverage.days.at(-1).date, `${saved.year}-12-31`);
  saved.days.forEach(day => {
    const comparison = speciesComparison(day.cumulative_species, coverage, day.date);
    assert.ok(comparison && comparison.gap >= 0);
    assert.ok(comparison.percentage === null || comparison.percentage <= 100);
  });
  const end = speciesComparison(saved.species_total, coverage, `${saved.year}-12-31`);
  assert.equal(end.observed, coverage.annual_species);
  assert.equal(end.gap, coverage.annual_species - saved.species_total);
});

test('real Alabama comparison matches all 366 route dates and never understates source coverage', {skip: !process.env.BIGYEAR_COMPARISON_ROUTE}, () => {
  const route = JSON.parse(readFileSync(process.env.BIGYEAR_COMPARISON_ROUTE, 'utf8'));
  assert.equal(route.observer_coverage.annual_species, 344);
  assert.equal(route.observer_coverage.days.length, 366);
  route.days.forEach(day => {
    const comparison = speciesComparison(day.cumulative_species, route.observer_coverage, day.date);
    assert.ok(comparison);
    assert.ok(comparison.gap >= 0);
    assert.ok(comparison.percentage === null || comparison.percentage <= 100);
  });
  const end = speciesComparison(route.species_total, route.observer_coverage, '2024-12-31');
  assert.equal(end.observed, 344);
  assert.equal(end.gap, 1);
  assert.equal(end.percentage.toFixed(1), '99.7');
});
