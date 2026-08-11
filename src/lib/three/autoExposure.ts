import * as THREE from "three";

const SAMPLE_TILE_SIZE = 8;
const SAMPLE_GRID_COLUMNS = 3;
const SAMPLE_GRID_ROWS = 3;
const SAMPLE_INTERVAL = 0.1;
const MIN_EXPOSURE = 0.5;
const MAX_EXPOSURE = 3;
const MIDDLE_GREY = 0.55;
const DARK_ADAPTATION_SPEED = 3;
const BRIGHT_ADAPTATION_SPEED = 4;

export class AutoExposure {
  private readonly pixels = new Uint8Array(SAMPLE_TILE_SIZE * SAMPLE_TILE_SIZE * 4);
  private readonly drawingBufferSize = new THREE.Vector2();
  private sampleElapsed = 0;
  private targetExposure = 1;

  constructor(private readonly renderer: THREE.WebGLRenderer) {}

  update(delta: number, enabled: boolean) {
    if (enabled === false) {
      this.targetExposure = 1;
      this.renderer.toneMappingExposure = 1;
      this.sampleElapsed = 0;
      return;
    }

    const currentExposure = this.renderer.toneMappingExposure;
    const adaptationSpeed =
      this.targetExposure < currentExposure ? BRIGHT_ADAPTATION_SPEED : DARK_ADAPTATION_SPEED;
    const blend = 1 - Math.exp(-adaptationSpeed * delta);
    this.renderer.toneMappingExposure = THREE.MathUtils.lerp(
      currentExposure,
      this.targetExposure,
      blend,
    );

    this.sampleElapsed += delta;
    if (this.sampleElapsed < SAMPLE_INTERVAL) return;
    this.sampleElapsed %= SAMPLE_INTERVAL;
    this.sampleFrameBuffer();
  }

  private sampleFrameBuffer() {
    this.renderer.getDrawingBufferSize(this.drawingBufferSize);
    const width = Math.min(SAMPLE_TILE_SIZE, this.drawingBufferSize.x);
    const height = Math.min(SAMPLE_TILE_SIZE, this.drawingBufferSize.y);
    if (width === 0 || height === 0) return;

    const context = this.renderer.getContext();
    let logarithmicLuminance = 0;
    let pixelCount = 0;

    for (let row = 0; row < SAMPLE_GRID_ROWS; row += 1) {
      for (let column = 0; column < SAMPLE_GRID_COLUMNS; column += 1) {
        const centerX = ((column + 0.5) / SAMPLE_GRID_COLUMNS) * this.drawingBufferSize.x;
        const centerY = ((row + 0.5) / SAMPLE_GRID_ROWS) * this.drawingBufferSize.y;
        const x = Math.max(
          0,
          Math.min(this.drawingBufferSize.x - width, Math.floor(centerX - width / 2)),
        );
        const y = Math.max(
          0,
          Math.min(this.drawingBufferSize.y - height, Math.floor(centerY - height / 2)),
        );

        context.readPixels(x, y, width, height, context.RGBA, context.UNSIGNED_BYTE, this.pixels);

        const tilePixelCount = width * height;
        pixelCount += tilePixelCount;
        for (let index = 0; index < tilePixelCount * 4; index += 4) {
          const red = this.pixels[index] / 255;
          const green = this.pixels[index + 1] / 255;
          const blue = this.pixels[index + 2] / 255;
          const luminance = red * 0.2126 + green * 0.7152 + blue * 0.0722;
          logarithmicLuminance += Math.log(Math.max(luminance, 0.001));
        }
      }
    }

    const averageLuminance = Math.exp(logarithmicLuminance / pixelCount);
    const exposureCorrection = MIDDLE_GREY / Math.max(averageLuminance, 0.001);
    this.targetExposure = THREE.MathUtils.clamp(
      this.renderer.toneMappingExposure * exposureCorrection,
      MIN_EXPOSURE,
      MAX_EXPOSURE,
    );
  }
}
