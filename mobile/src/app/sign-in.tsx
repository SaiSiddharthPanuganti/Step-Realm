/**
 * Sign in or create an account.
 *
 * One screen with a mode toggle rather than two screens, because the two forms
 * are the same three fields and a player who chose the wrong one should be able
 * to switch without losing what they typed.
 */

import { useState } from 'react';
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
import { API_BASE_URL } from '../api/runtime';
import { colours, radius, space, type } from '../ui/theme';

type Mode = 'sign-in' | 'sign-up';

export default function SignInScreen() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>('sign-in');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  const busy = useSession((state) => state.busy);
  const error = useSession((state) => state.error);
  const signIn = useSession((state) => state.signIn);
  const signUp = useSession((state) => state.signUp);

  async function submit() {
    const credentials = { email: email.trim(), password };
    const ok = mode === 'sign-in'
      ? await signIn(credentials)
      : await signUp(credentials);
    // The session store is the source of truth and `index` redirects on it, so
    // navigating from here as well would race that redirect. This replace keeps
    // the sign-in screen out of the back stack once there is a session.
    if (ok) {
      router.replace('/map');
    }
  }

  const canSubmit = email.trim().length > 0 && password.length > 0 && !busy;

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
        <Text style={styles.tagline}>
          Walk to earn mana. Spend it to take the ground you are standing on.
        </Text>

        <View style={styles.tabs}>
          <Tab
            label="Sign in"
            active={mode === 'sign-in'}
            onPress={() => setMode('sign-in')}
          />
          <Tab
            label="Create account"
            active={mode === 'sign-up'}
            onPress={() => setMode('sign-up')}
          />
        </View>

        <Field
          label="Email"
          value={email}
          onChangeText={setEmail}
          placeholder="you@example.com"
          keyboardType="email-address"
          autoCapitalize="none"
          autoComplete="email"
          // Off by default: a password manager filling this in without the
          // player noticing is how someone ends up in the wrong account.
          autoCorrect={false}
        />

        <Field
          label="Password"
          value={password}
          onChangeText={setPassword}
          placeholder={mode === 'sign-up' ? 'At least 8 characters' : 'Your password'}
          secureTextEntry
          autoCapitalize="none"
          autoComplete={mode === 'sign-up' ? 'new-password' : 'current-password'}
          onSubmitEditing={submit}
          returnKeyType="go"
        />

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
            {busy
              ? 'Working…'
              : mode === 'sign-in'
                ? 'Sign in'
                : 'Create account'}
          </Text>
        </Pressable>

        <Text style={styles.endpoint}>{API_BASE_URL}</Text>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

function Tab({
  label,
  active,
  onPress,
}: {
  label: string;
  active: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      accessibilityRole="tab"
      accessibilityState={{ selected: active }}
      style={[styles.tab, active && styles.tabActive]}
      onPress={onPress}
    >
      <Text style={[styles.tabLabel, active && styles.tabLabelActive]}>{label}</Text>
    </Pressable>
  );
}

function Field({
  label,
  ...inputProps
}: { label: string } & React.ComponentProps<typeof TextInput>) {
  return (
    <View style={styles.field}>
      <Text style={styles.fieldLabel}>{label}</Text>
      <TextInput
        {...inputProps}
        style={styles.input}
        placeholderTextColor={colours.textFaint}
      />
    </View>
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
  tabs: {
    flexDirection: 'row',
    backgroundColor: colours.surface,
    borderRadius: radius.md,
    padding: space.xs,
    gap: space.xs,
  },
  tab: {
    flex: 1,
    paddingVertical: space.sm,
    borderRadius: radius.sm,
    alignItems: 'center',
  },
  tabActive: {
    backgroundColor: colours.surfaceRaised,
  },
  tabLabel: {
    ...type.bodyStrong,
    color: colours.textMuted,
  },
  tabLabelActive: {
    color: colours.text,
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
  endpoint: {
    ...type.caption,
    color: colours.textFaint,
    textAlign: 'center',
  },
});
