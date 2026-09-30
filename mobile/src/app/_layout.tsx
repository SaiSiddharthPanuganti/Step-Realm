/**
 * Root layout: providers, and the one-time session check.
 *
 * The session restore is started here rather than on a screen, because it has
 * to happen before any screen can decide what to show. Starting it per-screen
 * would mean every entry point -- deep link, cold start, resume from background
 * -- has to remember to do it, and the one that forgets is the one that shows
 * the sign-in screen to someone who is signed in.
 */

import { useEffect } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { useSession } from '../auth/session';
import { API_BASE_URL } from '../api/runtime';
import { colours, space, type } from '../ui/theme';

/**
 * One client for the app's lifetime.
 *
 * Defaults are overridden deliberately on two points. `refetchOnWindowFocus` is
 * off because this is a phone: there is no window focus, and a backgrounded app
 * that refetches on return will spend the player's data. And `retry` is set to
 * leave the decision to each query, because a claim that failed with "not
 * adjacent" must not be retried, while a world fetch that failed on a dead
 * connection should be.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: false,
    },
  },
});

export default function RootLayout() {
  const restore = useSession((state) => state.restore);
  const status = useSession((state) => state.status);

  useEffect(() => {
    void restore();
  }, [restore]);

  return (
    <QueryClientProvider client={queryClient}>
      <SafeAreaProvider>
        <StatusBar style="light" />
        {status === 'restoring' ? (
          <Restoring />
        ) : (
          <Stack
            screenOptions={{
              headerShown: false,
              contentStyle: { backgroundColor: colours.background },
              animation: 'fade',
            }}
          >
            <Stack.Screen name="index" />
            <Stack.Screen name="sign-in" options={{ presentation: 'modal' }} />
            <Stack.Screen name="display-name" />
            <Stack.Screen name="map" />
            <Stack.Screen name="profile" />
            <Stack.Screen name="leaderboard" />
          </Stack>
        )}
      </SafeAreaProvider>
    </QueryClientProvider>
  );
}

/**
 * Shown while the stored token is checked.
 *
 * Deliberately says what it is doing. A blank screen during startup reads as a
 * crash, and the player has no way to tell a slow check from a broken app.
 */
function Restoring() {
  return (
    <View style={styles.restoring}>
      <Text style={styles.brand}>StepRealm</Text>
      <Text style={styles.status}>Checking your session…</Text>
      <Text style={styles.endpoint}>{API_BASE_URL}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  restoring: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.md,
    backgroundColor: colours.background,
    padding: space.xl,
  },
  brand: {
    ...type.display,
    color: colours.text,
  },
  status: {
    ...type.body,
    color: colours.textMuted,
  },
  endpoint: {
    ...type.caption,
    color: colours.textFaint,
  },
});
