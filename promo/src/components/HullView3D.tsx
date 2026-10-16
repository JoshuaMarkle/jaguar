import React, { useLayoutEffect, useMemo } from 'react';
import { ThreeCanvas } from '@remotion/three';
import { useThree } from '@react-three/fiber';
import * as THREE from 'three';
import { color, hull3d } from '../tokens';
import { blendRings, hullRings, type Hull, type Rings } from '../hull/loft';

export type HullInput = { hull: Hull; depth: number; flare: number };

type Props = {
  width: number;
  height: number;
  /** Real hulls. `morph` walks through them: 0 = first, 1 = second, 2.5 = halfway from third to fourth. */
  hulls: HullInput[];
  morph?: number;
  /** 0..1 opacity of the soft-gray solid. */
  solid?: number;
  /** Hairline wireframe (station rings + girth lines): 0 hidden, 1 the standard faint ink, up to ~3 for wireframe-only views. */
  wire?: number;
  /** Outline (sheers, keel, stems): ink, or accent for the winner. */
  edge?: 'ink' | 'accent' | 'none';
  edgeOpacity?: number;
  /** Camera: degrees around the hull from the starboard beam toward the bow, and above the water. */
  azimuth?: number;
  elevation?: number;
  /** Frame this length (in) in view. Fixed while a hull lengthens, so the change shows. */
  frameLength?: number;
  /** Multiplies the fitted camera distance (< 1 = closer). */
  zoom?: number;
  fov?: number;
  /** Station rings drawn in the wireframe: every k-th station. */
  ringEvery?: number;
  /** Girth lines drawn in the wireframe: every k-th girth point. */
  girthEvery?: number;
  /** Shift the look-at point along the hull, in (positive = toward the stern). */
  panX?: number;
};

const N = 161;
const NG = 25;

/**
 * A jaguar-hull/1 canoe in three.js, built with the web UI's own surface code (src/hull/loft.ts):
 * mirrored half girths from sheer to keel, flared topsides to the mold depth, stem arcs. Morphing
 * blends two REAL hulls vertex for vertex on a fixed station / girth parameterisation.
 */
export const HullView3D: React.FC<Props> = (props) => {
  const { width, height } = props;
  return (
    <ThreeCanvas width={width} height={height} flat gl={{ antialias: true, alpha: true, preserveDrawingBuffer: true }} camera={{ fov: props.fov ?? 18, near: 1, far: 50000 }}>
      <HullScene {...props} />
    </ThreeCanvas>
  );
};

const HullScene: React.FC<Props> = ({
  hulls,
  morph = 0,
  solid = 1,
  wire = 1,
  edge = 'ink',
  edgeOpacity = 1,
  azimuth = 35,
  elevation = 22,
  frameLength,
  zoom = 1,
  ringEvery = 8,
  girthEvery = 4,
  panX = 0,
  width,
  height,
}) => {
  const ringSets = useMemo(() => hulls.map((h) => hullRings(h.hull, h.depth, h.flare, N, NG)), [hulls]);
  const k = Math.max(0, Math.min(ringSets.length - 1, morph));
  const i0 = Math.min(ringSets.length - 1, Math.floor(k));
  const i1 = Math.min(ringSets.length - 1, i0 + 1);
  const rings: Rings = blendRings(ringSets[i0], ringSets[i1], k - i0);

  // Static topology: two mirrored sides of N x NG vertices.
  const index = useMemo(() => {
    const idx: number[] = [];
    for (let s = 0; s < 2; s++) {
      const base = s * N * NG;
      for (let i = 0; i < N - 1; i++)
        for (let g = 0; g < NG - 1; g++) {
          const a = base + i * NG + g;
          const b = a + NG;
          idx.push(a, b, b + 1, a, b + 1, a + 1);
        }
    }
    return idx;
  }, []);

  const { surface, wires, edges } = useMemo(() => {
    const pos = new Float32Array(2 * N * NG * 3);
    const at = (s: number, i: number, g: number): [number, number, number] => {
      const o = (i * NG + g) * 2;
      return [rings.xs[i], rings.ring[o + 1], s * rings.ring[o]];
    };
    let p = 0;
    for (const s of [1, -1])
      for (let i = 0; i < N; i++)
        for (let g = 0; g < NG; g++) {
          const v = at(s, i, g);
          pos[p++] = v[0];
          pos[p++] = v[1];
          pos[p++] = v[2];
        }
    const surface = new THREE.BufferGeometry();
    surface.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    surface.setIndex(index);
    surface.computeVertexNormals();

    const seg: number[] = [];
    const push = (a: [number, number, number], b: [number, number, number]) => seg.push(...a, ...b);
    // Station rings across both sides (keel to keel through the sheers).
    for (let i = 0; i < N; i += ringEvery)
      for (const s of [1, -1]) for (let g = 0; g < NG - 1; g++) push(at(s, i, g), at(s, i, g + 1));
    // Girth lines along the hull.
    for (let g = girthEvery; g < NG - 1; g += girthEvery)
      for (const s of [1, -1]) for (let i = 0; i < N - 1; i++) push(at(s, i, g), at(s, i + 1, g));
    const wires = new THREE.BufferGeometry();
    wires.setAttribute('position', new THREE.Float32BufferAttribute(seg, 3));

    const e: number[] = [];
    const pe = (a: [number, number, number], b: [number, number, number]) => e.push(...a, ...b);
    for (const s of [1, -1]) for (let i = 0; i < N - 1; i++) pe(at(s, i, 0), at(s, i + 1, 0)); // sheers
    for (let i = 0; i < N - 1; i++) pe(at(1, i, NG - 1), at(1, i + 1, NG - 1)); // keel
    for (const i of [0, N - 1]) for (let g = 0; g < NG - 1; g++) pe(at(1, i, g), at(1, i, g + 1)); // stems
    const edges = new THREE.BufferGeometry();
    edges.setAttribute('position', new THREE.Float32BufferAttribute(e, 3));
    return { surface, wires, edges };
  }, [rings, index, ringEvery, girthEvery]);

  const L = frameLength ?? Math.max(...ringSets.map((r) => r.length));
  const depth = rings.depth;
  return (
    <>
      <CameraRig length={L} centerX={rings.xs[0] + rings.length / 2} depth={depth} azimuth={azimuth} elevation={elevation} zoom={zoom} panX={panX} aspect={width / height} />
      <ambientLight intensity={hull3d.ambient} />
      <directionalLight position={[1, 2, 1.5]} intensity={hull3d.sun} />
      {solid > 0 ? (
        <mesh geometry={surface}>
          <meshLambertMaterial color={hull3d.solid} side={THREE.DoubleSide} transparent={solid < 1} opacity={solid} depthWrite={solid >= 1} polygonOffset polygonOffsetFactor={1} polygonOffsetUnits={1} />
        </mesh>
      ) : null}
      {wire > 0 ? (
        <lineSegments geometry={wires}>
          <lineBasicMaterial color={hull3d.wire} transparent opacity={Math.min(1, hull3d.wireOpacity * wire)} depthWrite={false} />
        </lineSegments>
      ) : null}
      {edge !== 'none' && edgeOpacity > 0 ? (
        <lineSegments geometry={edges}>
          <lineBasicMaterial color={edge === 'accent' ? color.accent : hull3d.edge} transparent={edgeOpacity < 1} opacity={edgeOpacity} />
        </lineSegments>
      ) : null}
    </>
  );
};

/** Places the camera every frame: target amidships of the current hull, distance fitted to frame `length`. */
const CameraRig: React.FC<{ length: number; centerX: number; depth: number; azimuth: number; elevation: number; zoom: number; panX: number; aspect: number }> = ({
  length,
  centerX,
  depth,
  azimuth,
  elevation,
  zoom,
  panX,
  aspect,
}) => {
  const camera = useThree((s) => s.camera) as THREE.PerspectiveCamera;
  useLayoutEffect(() => {
    const fov = (camera.fov * Math.PI) / 180;
    const hfov = 2 * Math.atan(Math.tan(fov / 2) * aspect);
    const dist = ((length * 0.56) / Math.tan(hfov / 2)) * zoom;
    const az = (azimuth * Math.PI) / 180;
    const el = (elevation * Math.PI) / 180;
    const target = new THREE.Vector3(centerX + panX, depth * 0.45, 0);
    // azimuth 0 = looking from the starboard beam (+z); positive turns toward the bow (x = 0).
    camera.position.set(target.x - dist * Math.cos(el) * Math.sin(az), target.y + dist * Math.sin(el), target.z + dist * Math.cos(el) * Math.cos(az));
    camera.up.set(0, 1, 0);
    camera.lookAt(target);
    camera.updateProjectionMatrix();
  }, [camera, length, centerX, depth, azimuth, elevation, zoom, panX, aspect]);
  return null;
};
