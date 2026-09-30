/**
 * The seam between the game and whichever map engine draws it.
 *
 * `GoogleMapsMap` is the react-native-maps implementation of the contract
 * below. The contract is deliberately small: everything above it deals in
 * ownership and coordinates, nothing above it knows what a hex is shaped like
 * or how a map shows one.
 */

import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from 'react';
import { Animated, Easing, StyleSheet, Text, View } from 'react-native';
import MapView, {
  Circle,
  Marker,
  Polygon,
  PROVIDER_GOOGLE,
  type LatLng,
  type Region,
} from 'react-native-maps';

import { lonLatToWorld, worldToLonLat, type FeatureCollection, type LonLat } from './geojson';
import type { WorldBounds } from '../api/types';
import { colours, radius, space, type } from '../ui/theme';

/**
 * Transparent enough that the street map shows through, opaque enough that the
 * ownership read. The Google renderer wants 8-digit ARGB hex; the theme colours
 * are opaque 6-digit, so an alpha byte (0x59 ≈ 35 %) is prefixed at draw time.
 *
 * Rival ground is deliberately the strongest fill: another player's territory
 * is the one thing the board must not let you miss, so it reads above your own
 * blue even though it is not yours. Unclaimed is the faintest -- it is the
 * background of the board, not a label.
 */
const OWNERSHIP_ALPHA = {
  own: '59',
  rival: '77',
  unclaimed: '3D',
} as const;

function withAlpha(colour: string, alpha: string): string {
  return `#${alpha}${colour.slice(1)}`;
}

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
  /**
   * Which way the player faces, degrees clockwise from north. Null before the
   * first fix, or when the chip and the fixes both have nothing to say.
   */
  playerHeading: number | null;
  /**
   * Radius of the fix's uncertainty in metres, when known. Drawn as a faint
   * ground ring so the player can see whether their circle fits inside the hex
   * they are about to claim -- walking progress is only worth as much as the
   * fix that measured it.
   */
  playerAccuracy: number | null;
  /**
   * The closed ring of the hex the player is currently standing in, so the
   * walk-gate has something to draw that the world response may not include
   * (their hex can sit outside the requested viewport).
   */
  currentHexRing: LonLat[] | null;
  /** A hex was tapped. Carries `(q, r)` exactly as the server sent it. */
  onSelectTile: (q: number, r: number) => void;
  /** The player panned or zoomed; the viewport should be refetched. */
  onViewportChange: (centre: LonLat) => void;
  children?: ReactNode;
}

/** A GeoJSON `[lon, lat]` pair turned into the `LatLng` a camera wants. */
function toLatLng([lon, lat]: LonLat): LatLng {
  return { latitude: lat, longitude: lon };
}

/**
 * A `WorldBounds` turned into the region the map should open on.
 *
 * A touch of padding past each edge, so the hexes are not glued to the edge of
 * the screen and the player can see the territory they came to look at.
 */
function toInitialRegion(bounds: WorldBounds): Region {
  const pad = 0.1;
  const lat = (bounds.min_lat + bounds.max_lat) / 2;
  const lon = (bounds.min_lon + bounds.max_lon) / 2;
  return {
    latitude: lat,
    longitude: lon,
    latitudeDelta: (bounds.max_lat - bounds.min_lat) * (1 + 2 * pad),
    longitudeDelta: (bounds.max_lon - bounds.min_lon) * (1 + 2 * pad),
  };
}

/**
 * One ownership layer: a polygon per hex.
 *
 * react-native-maps has no GeoJSON source, so a tile layer is one `Polygon`
 * per feature rather than one styled collection. That is fine at this scale --
 * a few rings of hexes is tens of polygons -- and it makes the tap handler
 * trivial: each polygon carries its own `(q, r)` in the closure, so there is
 * no lookup and no coordinate-to-tile math that could disagree with the
 * server's projection.
 */
function OwnershipLayer({
  shape,
  colour,
  alpha,
  onSelectTile,
}: {
  shape: FeatureCollection;
  colour: string;
  alpha: string;
  onSelectTile: MapProps['onSelectTile'];
}) {
  return (
    <>
      {shape.features.map((feature) => (
        <Polygon
          key={feature.id}
          coordinates={feature.geometry.coordinates[0].map(toLatLng)}
          fillColor={withAlpha(colour, alpha)}
          // A hairline in the theme's background colour, so the hexagons read
          // as units even where two tiles of the same ownership touch.
          strokeColor="rgba(11, 18, 32, 0.55)"
          strokeWidth={1}
          tappable
          onPress={() => onSelectTile(feature.properties.q, feature.properties.r)}
        />
      ))}
    </>
  );
}

/**
 * The map engine.
 *
 * Renders the three ownership layers as tappable polygons, draws the player as
 * a puck with a live heading arrow, and translates map events back into the
 * contract: a tap on a hex becomes the `(q, r)` the server sent, a pan/zoom
 * reports the new centre so the viewport can be refetched.
 *
 * The Google provider is explicit rather than relied on by default: the app
 * treats the hex grid as its geometry, and only Google's renderer on Android
 * will draw it consistently with the server's Web Mercator projection.
 */
export const GoogleMapsMap = forwardRef<MapHandle, MapProps>(function GoogleMapsMap(
  {
    layers,
    bounds,
    playerPosition,
    playerHeading,
    playerAccuracy,
    currentHexRing,
    onSelectTile,
    onViewportChange,
  },
  ref,
) {
  const mapRef = useRef<MapView | null>(null);
  // Seeded once from the first fetch's bounds: the camera is the player's, and
  // re-fitting it every time the world refetches would fight every pan.
  const [initialRegion] = useState(() => toInitialRegion(bounds));

  useImperativeHandle(
    ref,
    () => ({
      moveTo: (point: LonLat) => {
        mapRef.current?.animateCamera({ center: toLatLng(point) });
      },
      fitBounds: (next: WorldBounds) => {
        mapRef.current?.fitToCoordinates(
          [
            { latitude: next.max_lat, longitude: next.min_lon },
            { latitude: next.min_lat, longitude: next.max_lon },
          ],
          { edgePadding: { top: 64, right: 64, bottom: 64, left: 64 }, animated: true },
        );
      },
    }),
    [],
  );

  return (
    <MapView
      ref={mapRef}
      style={styles.map}
      provider={PROVIDER_GOOGLE}
      initialRegion={initialRegion}
      loadingEnabled
      loadingBackgroundColor={colours.background}
      // The hex grid is the map: it must not rotate, and a pitched view would
      // break its look far worse than the flat one.
      rotateEnabled={false}
      pitchEnabled={false}
      onRegionChangeComplete={(region, details) => {
        // Only the player's own gestures move the viewport. The animated
        // camera calls from `moveTo`/`fitBounds` must not trigger a refetch.
        if (details?.isGesture === true) {
          onViewportChange([region.longitude, region.latitude]);
        }
      }}
    >
      <OwnershipLayer
        shape={layers.own}
        colour={colours.own}
        alpha={OWNERSHIP_ALPHA.own}
        onSelectTile={onSelectTile}
      />
      <OwnershipLayer
        shape={layers.rival}
        colour={colours.rival}
        alpha={OWNERSHIP_ALPHA.rival}
        onSelectTile={onSelectTile}
      />
      <OwnershipLayer
        shape={layers.unclaimed}
        colour={colours.unclaimed}
        alpha={OWNERSHIP_ALPHA.unclaimed}
        onSelectTile={onSelectTile}
      />

      {playerPosition !== null ? (
        <PlayerMarker
          position={playerPosition}
          heading={playerHeading}
          accuracy={playerAccuracy}
        />
      ) : null}

      {currentHexRing !== null ? (
        <Polygon
          coordinates={currentHexRing.map(toLatLng)}
          fillColor="rgba(56, 189, 248, 0.06)"
          strokeColor={colours.accent}
          strokeWidth={2.5}
        />
      ) : null}
    </MapView>
  );
});

/**
 * The player, drawn the way a navigation app draws "you".
 *
 * Four native layers do the work, so nothing here needs to re-snapshot a
 * marker view every frame:
 *
 * - a faint ground ring marks the fix's uncertainty (the accuracy circle);
 * - a pulsing ripple expands from the position, "you are live" in the same
 *   visual language as a GPS app;
 * - a human figure -- head, torso and two feet in the accent colour, with a
 *   soft ground shadow beneath -- marks the position itself. The feet sit
 *   exactly on the fix (the marker is anchored feet-down), so the figure reads
 *   as standing ON the tile rather than floating over it. It is deliberately a
 *   silhouette, never a face: at this zoom a body is a direction and a shape,
 *   and it must keep that abstraction at every map scale.
 *
 * Heading sits on its own marker ahead of the figure, translated down the
 * player's direction of travel and rotated by it (rotation is a native
 * property, so it stays fluid). The arrow is north-up in its local space, so
 * rotating it by the heading makes its tip point where the player faces and
 * walks. No heading yet (standing still, compass silent) means no arrow; the
 * figure alone is honest about a direction we do not know.
 */

/** How far ahead of the puck the direction arrow floats, in ground metres. */
const ARROW_AHEAD_METERS = 15;
/** Larger accuracies just flood the board; the number is still in the HUD. */
const MAX_ACCURACY_RING_METERS = 60;

/** Where the heading arrow should stand, for a position and a heading. */
function arrowAhead(position: LonLat, heading: number): LonLat {
  const radians = (heading * Math.PI) / 180;
  // Plane metres: east x, north y, heading measured clockwise from north.
  const [x, y] = lonLatToWorld(position[0], position[1]);
  return worldToLonLat(
    x + Math.sin(radians) * ARROW_AHEAD_METERS,
    y + Math.cos(radians) * ARROW_AHEAD_METERS,
  );
}

function PlayerMarker({
  position,
  heading,
  accuracy,
}: {
  position: LonLat;
  heading: number | null;
  accuracy: number | null;
}) {
  // The ripple is a JS-driven Animated value feeding a native Circle's radius
  // prop. A Circle is a vector the map engine draws directly, so updating it
  // every frame does not touch the marker-view snapshots at all. Stored via
  // useState (not useRef) because the animation value is read while rendering
  // -- the interpolate below -- and the refs lint forbids that for a useRef.
  const [ripple] = useState(() => new Animated.Value(0));

  useEffect(() => {
    const loop = Animated.loop(
      Animated.timing(ripple, {
        toValue: 1,
        duration: 2800,
        easing: Easing.out(Easing.quad),
        useNativeDriver: false,
      }),
    );
    loop.start();
    return () => loop.stop();
  }, [ripple]);

  const rippleRadius = ripple.interpolate({ inputRange: [0, 1], outputRange: [16, 46] });

  return (
    <>
      {accuracy !== null && accuracy >= 0 && accuracy <= MAX_ACCURACY_RING_METERS ? (
        <Circle
          center={toLatLng(position)}
          radius={accuracy}
          strokeWidth={1}
          strokeColor="rgba(56, 189, 248, 0.22)"
          fillColor="rgba(56, 189, 248, 0.05)"
        />
      ) : null}

      <AnimatedCircle
        center={toLatLng(position)}
        radius={rippleRadius}
        strokeWidth={2}
        strokeColor="rgba(56, 189, 248, 0.35)"
        fillColor="rgba(56, 189, 248, 0.06)"
      />

      <Marker
        coordinate={toLatLng(position)}
        anchor={{ x: 0.5, y: 1 }}
        // Static apart from position, so it is snapshotted once; only the
        // native polygon circles and the arrow's native rotation ever change.
        tracksViewChanges={false}
        zIndex={10}
      >
        <View style={styles.playerShell}>
          <View style={styles.playerHalo} />
          <View style={styles.humanShadow} />
          <View style={styles.humanFigure}>
            <View style={styles.humanHead} />
            <View style={styles.humanTorso} />
            <View style={styles.humanFeet}>
              <View style={styles.humanFoot} />
              <View style={styles.humanFoot} />
            </View>
          </View>
        </View>
      </Marker>

      {heading !== null ? (
        <Marker
          coordinate={toLatLng(arrowAhead(position, heading))}
          anchor={{ x: 0.5, y: 0.5 }}
          rotation={heading}
          tracksViewChanges={false}
          zIndex={20}
        >
          <View style={styles.arrowNode}>
            <View style={styles.arrowTriangle} />
          </View>
        </Marker>
      ) : null}
    </>
  );
}

const AnimatedCircle = Animated.createAnimatedComponent(Circle);

GoogleMapsMap.displayName = 'GoogleMapsMap';

/**
 * The placeholder shown when the map engine cannot run.
 *
 * Not a spinner and not an empty screen: a real summary of the viewport. The
 * counts come from the server's own data, so the game is legible end to end and
 * the one thing that cannot be built -- the map itself -- is the only thing
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
        The map engine did not start. Everything below is real data from the
        server.
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
  map: { flex: 1 },
  // The figure's hit area: the halo, shadow and figure all sit centre-low, and
  // the marker's anchor (0.5, 1) pins the feet to the exact fix.
  playerShell: {
    width: 56,
    height: 56,
    alignItems: 'center',
    justifyContent: 'flex-end',
  },
  // The soft glow under the figure: enough to read on any street map, opaque
  // enough not to hide where the hex border is.
  playerHalo: {
    position: 'absolute',
    width: 56,
    height: 56,
    borderRadius: 28,
    backgroundColor: 'rgba(56, 189, 248, 0.16)',
  },
  // The ground shadow. Wider and flatter than the body, so the figure reads as
  // standing on the tile: something that casts a shadow is something that is
  // resting on the ground, not painted on it.
  humanShadow: {
    position: 'absolute',
    bottom: 3,
    width: 30,
    height: 11,
    borderRadius: 5.5,
    backgroundColor: 'rgba(11, 18, 32, 0.3)',
  },
  // Head, torso, feet -- a silhouette, not a portrait. The accent figures as
  // one solid colour so it stays legible at every zoom and needs no art asset.
  humanFigure: {
    alignItems: 'center',
    position: 'relative',
    zIndex: 1,
  },
  humanHead: {
    width: 14,
    height: 14,
    borderRadius: 7,
    backgroundColor: colours.accent,
    borderWidth: 2,
    borderColor: colours.background,
  },
  humanTorso: {
    width: 18,
    height: 16,
    borderRadius: 8,
    backgroundColor: colours.accent,
    borderWidth: 2,
    borderColor: colours.background,
    marginTop: -6,
  },
  humanFeet: {
    flexDirection: 'row',
    gap: 3,
    marginTop: -1,
  },
  humanFoot: {
    width: 7,
    height: 6,
    borderRadius: 3,
    backgroundColor: colours.accent,
    borderWidth: 1,
    borderColor: colours.background,
  },
  arrowNode: {
    width: 18,
    height: 18,
    alignItems: 'center',
    justifyContent: 'center',
  },
  // A north-pointing wedge when unredeemed: rotating the marker by the heading
  // is what aims its tip at the direction of travel.
  arrowTriangle: {
    width: 0,
    height: 0,
    borderLeftWidth: 8,
    borderRightWidth: 8,
    borderBottomWidth: 15,
    borderLeftColor: 'transparent',
    borderRightColor: 'transparent',
    borderBottomColor: colours.accent,
  },
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