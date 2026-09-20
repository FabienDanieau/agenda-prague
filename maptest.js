/* Headless check of the map view's data plumbing: no browser, just the pure logic. */
const fs = require('fs');
const DATA = JSON.parse(fs.readFileSync(__dirname + '/events.json', 'utf8'));

const places = DATA.places || {};
console.log(`events: ${DATA.count}, venues: ${DATA.venues.length}, placed: ${Object.keys(places).length}`);

// group by day exactly as renderMap does
const byDay = new Map();
for (const e of DATA.events) (byDay.get(e.date) || byDay.set(e.date, []).get(e.date)).push(e);
const days = [...byDay.keys()].sort();
console.log(`days with events: ${days.length}, first: ${days[0]}, last: ${days[days.length-1]}`);

let placed = 0, unplaced = 0;
const missing = new Map();
for (const e of DATA.events) {
  if (places[e.venue]) placed++;
  else { unplaced++; missing.set(e.venue, (missing.get(e.venue) || 0) + 1); }
}
console.log(`on the map: ${placed}/${DATA.count} (${(100*placed/DATA.count).toFixed(1)}%)`);

// every coordinate must actually be in Prague
const bad = Object.entries(places).filter(([, [la, ln]]) =>
  Math.abs(la - 50.0755) > 0.3 || Math.abs(ln - 14.4378) > 0.4);
console.log(`coords outside Prague: ${bad.length}`, bad.slice(0, 3));

// busiest day, as the strip would show it
const busiest = days.map(d => [d, byDay.get(d).length]).sort((a, b) => b[1] - a[1])[0];
const venuesThatDay = new Set(byDay.get(busiest[0]).filter(e => places[e.venue]).map(e => e.venue));
console.log(`busiest day ${busiest[0]}: ${busiest[1]} events across ${venuesThatDay.size} pins`);

console.log('top unplaced venues:', [...missing].sort((a,b)=>b[1]-a[1]).slice(0, 6));
