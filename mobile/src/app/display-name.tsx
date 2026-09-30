/**
 * The naming gate: choose the display name shown on your territory.
 *
 * Every account arrives here at least once, straight after signup. The server
 * stamps a derived name on every account so one always exists (the map needs
 * something to label with), but a label someone else guessed from an email
 * address is not a name a player has chosen -- so this screen is where the
 * choice happens, and it cannot be skipped with a blank.
 *
 * The server is the authority. The field is pre-checked locally so a player
 * gets instant feedback on characters and length, but the uniqueness rule can
 * only be judged by the server, and a 409 answer is shown verbatim rather than
 * paraphrased.
 */

import { useEffect, useState } from 'react';
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useRouter } from 'expo-router';

import { useSession } from '../auth/session';
import { colours, radius, space, type } from '../ui/theme';

/** Letters, digits and underscores -- the same class the server accepts. */
const NAME_LENGTH_MIN = 3;
const NAME_LENGTH_MAX = 24;

function isValidName(name: string): boolean {
  return name.length >= NAME_LENGTH_MIN &&
    name.length <= NAME_LENGTH_MAX &&
    /^[A-Za-z0-9_]+$/.test(name);
}

export default function DisplayNameScreen() {
  const router = useRouter();
  const status = useSession((state) => state.status);
  const user = useSession((state) => state.user);
  const setDisplayName = useSession((state) => state.setDisplayName);

  const [name, setName] = useState(user?.display_name ?? '');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // A session cannot survive this screen untouched -- a 401 anywhere signs the
  // player out -- and there is nothing to name if nobody is signed in.
  useEffect(() => {
    if (status === 'signed-out') {
      router.replace('/sign-in');
    }
  }, [status, router]);

  const trimmed = name.trim();
  const canSubmit = isValidName(trimmed) && !busy;

  async function submit() {
    if (!canSubmit || user === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await setDisplayName(trimmed);
      // The session now owns the new label, and `index` sends a naming-complete
      // player to the map. A replace here would race that redirect, the same
      // rule as sign-in.
      router.replace('/');
    } catch (cause) {
      setError(
        cause instanceof Error && 'userMessage' in cause
          ? String((cause as { userMessage: string }).userMessage)
          : 'Could not save that name. Please try again.',
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <KeyboardAvoidingView
      style={styles.flex}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <ScrollView
        contentContainerStyle={styles.content}
        keyboardShouldPersistTaps="handled"
      >
        <Text style={styles.brand}>StepRealm</Text>
        <Text style={styles.tagline}>Choose how your territory is labelled.</Text>

        <View style={styles.field}>
          <Text style={styles.fieldLabel}>Display name</Text>
          <TextInput
            value={name}
            onChangeText={(raw) => {
              setName(raw);
              setError(null);
            }}
            placeholder="e.g. FirstWalker"
            autoCapitalize="none"
            autoCorrect={false}
            maxLength={NAME_LENGTH_MAX}
            style={styles.input}
            placeholderTextColor={colours.textFaint}
            returnKeyType="go"
            onSubmitEditing={submit}
          />
        </View>
        <Text style={styles.hint}>
          Letters, digits and underscores — shown on every tile you own.
        </Text>

        {error !== null ? <Text style={styles.error}>{error}</Text> : null}

        <Pressable
          accessibilityRole="button"
          accessibilityState={{ disabled: !canSubmit }}
          style={({ pressed }) => [
            styles.submit,
            !canSubmit && styles.submitDisabled,
            pressed && canSubmit && styles.submitPressed,
          ]}
          onPress={submit}
          disabled={!canSubmit}
        >
          <Text style={styles.submitLabel}>
            {busy ? 'Saving…' : 'Start walking'}
          </Text>
        </Pressable>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: colours.background },
  content: {
    flexGrow: 1,
    justifyContent: 'center',
    padding: space.xl,
    gap: space.lg,
  },
  brand: {
    ...type.display,
    color: colours.text,
  },
  tagline: {
    ...type.body,
    color: colours.textMuted,
    marginTop: -space.md,
  },
  field: {
    gap: space.xs,
  },
  fieldLabel: {
    ...type.label,
    color: colours.textMuted,
  },
  input: {
    ...type.body,
    color: colours.text,
    backgroundColor: colours.surface,
    borderWidth: 1,
    borderColor: colours.border,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
  },
  hint: {
    ...type.caption,
    color: colours.textFaint,
    marginTop: -space.sm,
  },
  error: {
    ...type.body,
    color: colours.danger,
  },
  submit: {
    backgroundColor: colours.accent,
    borderRadius: radius.md,
    paddingVertical: space.lg,
    alignItems: 'center',
  },
  submitDisabled: {
    backgroundColor: colours.surfaceRaised,
  },
  submitPressed: {
    opacity: 0.8,
  },
  submitLabel: {
    ...type.bodyStrong,
    color: colours.background,
  },
});