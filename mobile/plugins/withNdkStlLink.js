const fs = require('fs/promises');
const path = require('path');

const { withDangerousMod } = require('expo/config-plugins');

/**
 * Works around an NDK 27 toolchain bug that breaks every autolinked React Native
 * native module on Android.
 *
 * With NDK 27.1 (the version Expo SDK 57 defaults to) CMake receives
 * `-DANDROID_STL=c++_shared` but the toolchain never consumes it -- it shows up
 * in the cache as `ANDROID_STL:UNINITIALIZED=c++_shared`, meaning nothing read
 * it. Consequently no target links `libc++_shared.so` and the C++ link step dies
 * with hundreds of `undefined symbol: std::__ndk1::...` errors.
 *
 * Pinning NDK 26 is not an option: RN 0.86 headers require C++20 `std::format`,
 * which r26's libc++ does not have. Upstream reference, including the NDK
 * matrix showing no version works unpatched:
 *   https://github.com/software-mansion/react-native-reanimated/issues/9444
 *   https://github.com/mikeartee/repro-ndk27-worklets
 *
 * The verified fix is to link the STL explicitly, so this adds `c++_shared` to
 * every `target_link_libraries(...)` in the native modules' CMakeLists.
 *
 * This is safe to delete once the NDK 27 regression is fixed upstream.
 */

/** node_modules layout is `<scope>/<pkg>/android` or `<pkg>/android`. */
function nativeModuleDirs(nodeModules) {
  return fs
    .readdir(nodeModules, { withFileTypes: true })
    .then((entries) =>
      Promise.all(
        entries.map(async (entry) => {
          if (!entry.isDirectory() || entry.name === '.bin') {
            return [];
          }
          if (entry.name.startsWith('@')) {
            const scopeDir = path.join(nodeModules, entry.name);
            const scoped = await fs.readdir(scopeDir, { withFileTypes: true });
            return Promise.all(
              scoped
                .filter((scopedEntry) => scopedEntry.isDirectory())
                .map((scopedEntry) =>
                  path.join(scopeDir, scopedEntry.name, 'android'),
                ),
            );
          }
          return [path.join(nodeModules, entry.name, 'android')];
        }),
      ),
    )
    .then((lists) => lists.flat());
}

/**
 * Every CMake file a native module can declare its link libraries in.
 *
 * Not just `CMakeLists.txt`: `expo-modules-core` declares none itself and instead
 * includes `android/cmake/*.cmake`, where the link happens inside
 * `use_expo_common()`. Patching only the top-level file leaves the whole Expo
 * module tree unlinked, and those files are the ones the build then fails on.
 */
async function cmakeFilesIn(androidDir) {
  let entries;
  try {
    entries = await fs.readdir(androidDir, { withFileTypes: true });
  } catch {
    return []; // Package has no android/ dir.
  }

  const found = [];
  for (const entry of entries) {
    const full = path.join(androidDir, entry.name);
    if (entry.isFile() && (entry.name === 'CMakeLists.txt' || entry.name.endsWith('.cmake'))) {
      found.push(full);
    } else if (
      entry.isDirectory() &&
      // Build output and caches are large and never contain source CMake.
      !entry.name.startsWith('.') &&
      entry.name !== 'build'
    ) {
      found.push(...(await cmakeFilesIn(full)));
    }
  }
  return found;
}

/**
 * Matches `${target_name}` as well as a bare name, so functions are covered.
 *
 * The scope keyword (`INTERFACE`/`PUBLIC`/`PRIVATE`) is captured rather than
 * skipped over: CMake requires it to be the second argument, so `c++_shared` has
 * to go after it, and "just after the target name" produces
 * "INTERFACE library can only be used with the INTERFACE keyword" at configure
 * time. The leading whitespace is re-emitted verbatim so multi-line calls keep
 * their formatting.
 */
const LINK_CALL =
  /target_link_libraries\((\s*(?:\$\{[A-Za-z0-9_]+\}|[A-Za-z0-9_-]+))(\s+(?:INTERFACE|PUBLIC|PRIVATE))?/g;

async function addSharedStl(cmakeFilePath, projectRoot) {
  let contents;
  try {
    contents = await fs.readFile(cmakeFilePath, 'utf8');
  } catch {
    return null;
  }

  if (!contents.includes('target_link_libraries')) {
    return null;
  }
  // Already linked, either upstream or by a previous prebuild.
  if (/target_link_libraries\([^)]*\bc\+\+_shared\b/.test(contents)) {
    return null;
  }

  const patched = contents.replace(
    LINK_CALL,
    (_match, name, scope) => `target_link_libraries(${name}${scope ?? ''} c++_shared`,
  );
  if (patched === contents) {
    return null;
  }

  await fs.writeFile(cmakeFilePath, patched);
  return path.relative(projectRoot, cmakeFilePath);
}

function withNdkStlLink(config) {
  return withDangerousMod(config, [
    'android',
    async (config) => {
      const projectRoot = config.modRequest.projectRoot;
      const nodeModules = path.join(projectRoot, 'node_modules');
      const dirs = await nativeModuleDirs(nodeModules);
      // The app module's own jni glue, when the template generates one.
      dirs.push(path.join(config.modRequest.platformProjectRoot, 'app', 'src', 'main', 'jni'));

      const patched = [];
      for (const dir of dirs) {
        for (const file of await cmakeFilesIn(dir)) {
          const result = await addSharedStl(file, projectRoot);
          if (result !== null) {
            patched.push(result);
          }
        }
      }

      // A silent no-op here means the workaround quietly stopped working and the
      // build fails much later with hundreds of linker errors, so say what
      // happened.
      console.log(
        `withNdkStlLink: linked c++_shared in ${patched.length} CMake files ` +
          `(NDK 27 workaround):\n  ${patched.join('\n  ')}`,
      );
      if (patched.length === 0) {
        console.warn(
          'withNdkStlLink: no CMake file needed patching. If the build fails ' +
            'with undefined std::__ndk1 symbols, this plugin needs updating.',
        );
      }

      return config;
    },
  ]);
}

module.exports = withNdkStlLink;
