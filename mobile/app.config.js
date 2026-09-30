/**
 * The static config is in `app.json`; this file only layers secrets on top.
 *
 * The Google Maps SDK API key must reach the generated Android manifest as the
 * `com.google.android.geo.API_KEY` meta-data. Putting it in `app.json` would
 * commit it to Git, and this project treats every API key as a credential it
 * does not follow you around in history. So the key is read from
 * `GOOGLE_MAPS_API_KEY` or, failing that, from `./google-maps-api-key` (a file
 * this repository ignores) and injected at prebuild time.
 *
 * Without a key the react-native-maps plugin is omitted entirely, so prebuild
 * still succeeds -- the map just cannot authenticate until the key exists.
 */
const fs = require('fs');
const path = require('path');

const KEY_FILE = path.join(__dirname, 'google-maps-api-key');

function loadGoogleMapsApiKey() {
  if (process.env.GOOGLE_MAPS_API_KEY) {
    return process.env.GOOGLE_MAPS_API_KEY;
  }
  try {
    return fs.readFileSync(KEY_FILE, 'utf8').trim();
  } catch {
    return undefined;
  }
}

module.exports = ({ config }) => {
  const apiKey = loadGoogleMapsApiKey();
  const plugins = [...(config.plugins ?? [])];

  if (apiKey) {
    plugins.push(['react-native-maps', { androidGoogleMapsApiKey: apiKey }]);
  }

  return { ...config, plugins };
};