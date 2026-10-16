import { Config } from '@remotion/cli/config';

// Frames as PNG so 1 px hairlines stay crisp before the H.264 encode.
Config.setVideoImageFormat('png');
Config.setOverwriteOutput(true);
Config.setCrf(16);
Config.setPixelFormat('yuv420p');
// three.js needs WebGL. 'angle' uses the GPU where there is one; on a machine without one set
// REMOTION_GL=swangle (software). REMOTION_BROWSER points at a local Chrome / headless shell
// when Remotion cannot download its own.
Config.setChromiumOpenGlRenderer((process.env.REMOTION_GL as 'angle' | 'swangle' | undefined) ?? 'angle');
if (process.env.REMOTION_BROWSER) Config.setBrowserExecutable(process.env.REMOTION_BROWSER);
