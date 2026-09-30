/**
 * The entry route. Decides where the app goes, and does nothing else.
 *
 * Redirecting here rather than in `_layout` keeps the gate in one place that
 * can be reasoned about: signed in goes to the map, otherwise to sign-in. The
 * alternative -- letting each screen check and redirect itself -- means a new
 * screen can forget, and the symptom is a signed-in player seeing a login form.
 */

import { Redirect } from 'expo-router';

import { useSession } from '../auth/session';

export default function Index() {
  const status = useSession((state) => state.status);
  const needsDisplayName = useSession((state) => state.needsDisplayName);

  // The root layout already blocks rendering on 'restoring', so by the time this
  // runs the answer is known. The guard is here because a redirect is a render
  // decision, and redirecting on an unknown state would send a signed-in player
  // to the sign-in screen on every cold start.
  if (status === 'restoring') {
    return null;
  }

  if (status !== 'signed-in') {
    return <Redirect href="/sign-in" />;
  }

  // The naming gate sits between sign-in and the map: a new account is not
  // allowed on the board until its public label is chosen.
  return <Redirect href={needsDisplayName ? '/display-name' : '/map'} />;
}
