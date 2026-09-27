// Lint configuration.
//
// Flat config, because ESLint 9 removed eslintrc. `eslint-config-expo/flat`
// supplies the React Native and Expo rules -- notably the hooks rules, which
// are the ones that catch a stale closure in an effect rather than a
// formatting preference.
const expoConfig = require('eslint-config-expo/flat');

module.exports = [
  ...expoConfig,
  {
    ignores: ['dist/**', '.expo/**', 'node_modules/**'],
  },
  {
    rules: {
      // The API layer is written to be free of side effects on import, so a
      // stray console.log is a debug statement that shipped.
      'no-console': ['warn', { allow: ['warn', 'error'] }],
    },
  },
];
