import { cancelRender, continueRender, delayRender, staticFile } from 'remotion';

// Inter variable (opsz + wght axes, latin) stands in for Saans; IBM Plex Mono for the terminal.
// Both SIL OFL 1.1 (public/fonts/*-OFL.txt). Rendering waits until both are loaded.
let started = false;

export function loadFonts(): void {
  if (started || typeof document === 'undefined') return;
  started = true;
  const handle = delayRender('Loading Inter and IBM Plex Mono');
  const faces = [
    new FontFace('Inter', `url('${staticFile('fonts/InterVariable-latin.woff2')}') format('woff2')`, {
      weight: '100 900',
      style: 'normal',
    }),
    new FontFace('IBM Plex Mono', `url('${staticFile('fonts/IBMPlexMono-400-latin.woff2')}') format('woff2')`, { weight: '400' }),
    new FontFace('IBM Plex Mono', `url('${staticFile('fonts/IBMPlexMono-600-latin.woff2')}') format('woff2')`, { weight: '600' }),
  ];
  Promise.all(faces.map((f) => f.load()))
    .then((loaded) => {
      loaded.forEach((f) => (document.fonts as unknown as { add: (face: FontFace) => void }).add(f));
      continueRender(handle);
    })
    .catch((err) => cancelRender(err));
}
