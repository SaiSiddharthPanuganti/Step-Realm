/**
 * Visual constants.
 *
 * One source rather than literals scattered through screens, so the palette can
 * change in one place and so two screens cannot end up with two greys that are
 * supposed to be the same. Dark by default: this is a map app used outdoors,
 * and a bright screen at night is both uncomfortable and a battery cost.
 */

export const colours = {
  /** Page background. Near-black with a blue cast, not pure black. */
  background: '#0B1220',
  /** Raised surfaces: cards, sheets, the mana bar's track. */
  surface: '#141D2E',
  surfaceRaised: '#1C2740',
  border: '#26334D',

  text: '#E8EEF9',
  /** Secondary text: labels, timestamps, hints. */
  textMuted: '#8A9BB8',
  textFaint: '#5A6B87',

  /** The player's own territory. */
  own: '#38BDF8',
  ownMuted: '#0E4A66',
  /** Territory belonging to someone else. */
  rival: '#F472B6',
  /** Unclaimed ground. */
  unclaimed: '#3E5170',

  accent: '#38BDF8',
  success: '#34D399',
  /** Mana, and the mana bar's fill. */
  mana: '#A78BFA',
  danger: '#F87171',
  warning: '#FBBF24',
} as const;

/**
 * An 8px scale.
 *
 * A scale rather than arbitrary numbers because arbitrary numbers are how a
 * layout ends up with 13px and 15px gaps on the same screen. `md` is the
 * default; anything larger is a deliberate separation.
 */
export const space = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const;

export const radius = {
  sm: 6,
  md: 10,
  lg: 16,
  pill: 999,
} as const;

/**
 * Sizes are in `fontSize` units with the line height alongside, because
 * React Native does not set a line height for you and the default differs
 * between platforms -- which is how the same text ends up tightly spaced on
 * Android and loosely spaced on iOS.
 */
export const type = {
  display: { fontSize: 34, lineHeight: 40, fontWeight: '700' },
  title: { fontSize: 22, lineHeight: 28, fontWeight: '700' },
  heading: { fontSize: 17, lineHeight: 22, fontWeight: '600' },
  body: { fontSize: 15, lineHeight: 21, fontWeight: '400' },
  bodyStrong: { fontSize: 15, lineHeight: 21, fontWeight: '600' },
  label: { fontSize: 13, lineHeight: 17, fontWeight: '500' },
  /** Tabular-ish figures for counters that change as you walk. */
  numeric: { fontSize: 20, lineHeight: 24, fontWeight: '700' },
  caption: { fontSize: 11, lineHeight: 15, fontWeight: '500' },
} as const;
