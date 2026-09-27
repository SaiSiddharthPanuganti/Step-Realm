/**
 * The seam between the game and whichever map engine draws it.
 *
 * This exists because the map engine is not decided yet and cannot be: Mappls
 * needs an account, an appId, an API key and four credential files, none of
 * which are available. Rather than block on that, the game logic -- viewport
 * state, ownership, claiming, mana -- is written against this interface, and
 * the engine becomes a detail behind it.
 *
 * The contract is deliberately small. Everything above it deals in ownership
 * and coordinates; nothing above it knows what a "GeoJSON source" is. When
 * Mappls lands, `MapplsMap` implements this and nothing else changes.
 */

import type { ReactNode } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import type { FeatureCollection, LonLat } from './geojson';
import type { WorldBounds } from '../api/types';
import { colours, radius, space, type } from '../ui/theme';

export interface MapHandle {
  /** Frame the camera on a position, keeping the current zoom. */
  moveTo(point: LonLat): void;
  /** Fit the camera to a bounding box, choosing a zoom to fit. */
  fitBounds(bounds: WorldBounds): void;
}

export interface MapProps {
  /** The three ownership layers, ready to draw. */
  layers: {
    own: FeatureCollection;
    rival: FeatureCollection;
    unclaimed: FeatureCollection;
  };
  /** The viewport the data was fetched for, for the initial camera. */
  bounds: WorldBounds;
  /** The player's own position, when the app has a fix. */
  playerPosition: LonLat | null;
  /** A hex was tapped. Carries `(q, r)` exactly as the server sent it. */
  onSelectTile: (q: number, r: number) => void;
  /** The player panned or zoomed; the viewport should be refetched. */
  onViewportChange: (centre: LonLat) => void;
  children?: ReactNode;
}

/**
 * The placeholder shown until Mappls credentials exist.
 *
 * Not a spinner and not an empty screen: a real summary of the viewport. The
 * counts come from the server's own data, so the game is legible end to end and
 * the one thing that cannot be built -- the vector map -- is the only thing
 * visibly missing.
 */
export function PlaceholderMap({ layers, playerPosition }: MapProps) {
  const rows = [
    { label: 'Yours', count: layers.own.features.length, colour: colours.own },
    { label: 'Claimed by others', count: layers.rival.features.length, colour: colours.rival },
    { label: 'Unclaimed', count: layers.unclaimed.features.length, colour: colours.unclaimed },
  ];
  const total = rows.reduce((sum, row) => sum + row.count, 0);

  return (
    <View style={styles.shell}>
      <Text style={styles.notice}>
        The map itself needs a Mappls API key. Everything below is real data from
        the server.
      </Text>

      <View style={styles.legend}>
        {rows.map((row) => (
          <View key={row.label} style={styles.row}>
            <View style={[styles.swatch, { backgroundColor: row.colour }]} />
            <Text style={styles.label}>{row.label}</Text>
            <Text style={styles.count}>{row.count}</Text>
          </View>
        ))}
      </View>

      <Text style={styles.total}>
        {total} hex{total === 1 ? '' : 'es'} in view
        {playerPosition === null ? '' : ' · you are here'}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  shell: {
    flex: 1,
    backgroundColor: colours.background,
    padding: space.lg,
    gap: space.lg,
  },
  notice: {
    ...type.body,
    color: colours.warning,
    backgroundColor: colours.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colours.border,
    padding: space.md,
  },
  legend: {
    gap: space.sm,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
  },
  swatch: {
    width: 14,
    height: 14,
    borderRadius: 3,
  },
  label: {
    ...type.body,
    color: colours.text,
    flex: 1,
  },
  count: {
    ...type.numeric,
    color: colours.textMuted,
  },
  total: {
    ...type.label,
    color: colours.textFaint,
  },
});
