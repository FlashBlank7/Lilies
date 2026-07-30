import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * JBR 21 adapter for the acceptance-oracle NumericReference algorithms.
 *
 * <p>This adapter is implementation evidence, not specification authority. The
 * inline acceptance-oracle pseudocode and toolchain-locked JBR are authoritative.</p>
 */
public final class NumericReference {
    private static final int INPUT_MAGIC = 0x5430314d;
    private static final int OUTPUT_MAGIC = 0x4e524546;
    private static final int VERSION = 1;
    private static final double HUBER_DELTA = 2.0d / 255.0d;

    private record Box(int left, int top, int right, int bottom) {}
    private record PixelScore(
            double distance,
            int y,
            int x,
            double[] actual,
            double[] background,
            double ratio
    ) {}
    private record CharacterResult(
            Box box,
            int ringCount,
            int glyphCount,
            int coreCount,
            double[][] coefficients,
            List<PixelScore> selected,
            double minimumRatio
    ) {}

    private NumericReference() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 1 || (!args[0].equals("lookup") && !args[0].equals("contrast"))) {
            throw new IllegalArgumentException("usage: NumericReference lookup|contrast");
        }
        try (DataOutputStream output = new DataOutputStream(
                new BufferedOutputStream(System.out))) {
            output.writeInt(OUTPUT_MAGIC);
            output.writeInt(VERSION);
            writeLookup(output);
            if (args[0].equals("contrast")) {
                runContrast(output);
            }
        }
    }

    private static void writeLookup(DataOutputStream output) throws Exception {
        output.writeInt(256);
        for (int q = 0; q <= 255; q++) {
            output.writeLong(raw(srgb(q)));
        }
    }

    private static void runContrast(DataOutputStream output) throws Exception {
        try (DataInputStream input = new DataInputStream(
                new BufferedInputStream(System.in))) {
            if (input.readInt() != INPUT_MAGIC || input.readInt() != VERSION) {
                throw new IllegalArgumentException("invalid NumericReference input");
            }
            int width = input.readInt();
            int height = input.readInt();
            if (width <= 0 || height <= 0
                    || ((long) width) * ((long) height) > 100_000_000L) {
                throw new IllegalArgumentException("invalid image dimensions");
            }
            int byteCount = input.readInt();
            if (byteCount != width * height * 3) {
                throw new IllegalArgumentException("invalid RGB byte count");
            }
            byte[] rgb = input.readNBytes(byteCount);
            if (rgb.length != byteCount) {
                throw new IllegalArgumentException("truncated RGB pixels");
            }
            int count = input.readInt();
            if (count <= 0 || count > 100_000) {
                throw new IllegalArgumentException("invalid character-box count");
            }
            Box[] boxes = new Box[count];
            for (int index = 0; index < count; index++) {
                double rawLeft = finite(input.readDouble());
                double rawTop = finite(input.readDouble());
                double rawRight = finite(input.readDouble());
                double rawBottom = finite(input.readDouble());
                Box box = new Box(
                        (int) StrictMath.floor(rawLeft),
                        (int) StrictMath.floor(rawTop),
                        (int) StrictMath.ceil(rawRight),
                        (int) StrictMath.ceil(rawBottom)
                );
                if (box.left < 0 || box.top < 0 || box.right > width
                        || box.bottom > height || box.right <= box.left
                        || box.bottom <= box.top) {
                    throw new IllegalArgumentException("invalid character box");
                }
                boxes[index] = box;
            }
            if (input.read() != -1) {
                throw new IllegalArgumentException("trailing NumericReference input");
            }
            double[] lookup = new double[256];
            for (int q = 0; q <= 255; q++) {
                lookup[q] = srgb(q);
            }
            output.writeInt(count);
            for (int index = 0; index < count; index++) {
                writeCharacter(
                        output,
                        measureCharacter(width, height, rgb, boxes, index, lookup)
                );
            }
        }
    }

    private static CharacterResult measureCharacter(
            int width,
            int height,
            byte[] rgb,
            Box[] boxes,
            int targetIndex,
            double[] lookup
    ) {
        Box box = boxes[targetIndex];
        List<int[]> ring = new ArrayList<>();
        for (int y = StrictMath.max(0, box.top - 3);
                y < StrictMath.min(height, box.bottom + 3); y++) {
            for (int x = StrictMath.max(0, box.left - 3);
                    x < StrictMath.min(width, box.right + 3); x++) {
                if (x >= box.left && x < box.right && y >= box.top && y < box.bottom) {
                    continue;
                }
                boolean excluded = false;
                for (int other = 0; other < boxes.length; other++) {
                    if (other != targetIndex && contains(boxes[other], x, y)) {
                        excluded = true;
                        break;
                    }
                }
                if (!excluded) {
                    ring.add(new int[] {x, y});
                }
            }
        }
        if (ring.size() < 24) {
            throw new IllegalArgumentException("character ring has fewer than 24 pixels");
        }
        int xlo = ring.get(0)[0];
        int xhi = xlo;
        int ylo = ring.get(0)[1];
        int yhi = ylo;
        for (int[] point : ring) {
            if (point[0] < xlo) xlo = point[0];
            if (point[0] > xhi) xhi = point[0];
            if (point[1] < ylo) ylo = point[1];
            if (point[1] > yhi) yhi = point[1];
        }
        if (xhi <= xlo || yhi <= ylo) {
            throw new IllegalArgumentException("degenerate retained ring coordinates");
        }
        double[][] coefficients = new double[3][];
        for (int channel = 0; channel < 3; channel++) {
            double[][] design = new double[ring.size()][4];
            double[] values = new double[ring.size()];
            for (int index = 0; index < ring.size(); index++) {
                int[] point = ring.get(index);
                design[index] = design(point[0], point[1], xlo, xhi, ylo, yhi);
                values[index] = lookup[channel(rgb, width, point[0], point[1], channel)];
            }
            coefficients[channel] = fit(design, values);
        }

        boolean[][] candidate = new boolean[box.bottom - box.top][box.right - box.left];
        for (int y = box.top; y < box.bottom; y++) {
            for (int x = box.left; x < box.right; x++) {
                double[] actual = linearPixel(rgb, width, x, y, lookup);
                double[] fitted = fitted(coefficients, design(x, y, xlo, xhi, ylo, yhi));
                candidate[y - box.top][x - box.left] =
                        distance(oklab(actual), oklab(clamp(fitted))) > 0.04d;
            }
        }
        boolean[][] denoised = new boolean[candidate.length][candidate[0].length];
        for (int y = 0; y < candidate.length; y++) {
            for (int x = 0; x < candidate[0].length; x++) {
                if (!candidate[y][x]) continue;
                int neighbors = 0;
                for (int dy = -1; dy <= 1; dy++) {
                    for (int dx = -1; dx <= 1; dx++) {
                        if (dx == 0 && dy == 0) continue;
                        int nx = x + dx;
                        int ny = y + dy;
                        if (ny >= 0 && ny < candidate.length
                                && nx >= 0 && nx < candidate[0].length
                                && candidate[ny][nx]) {
                            neighbors++;
                        }
                    }
                }
                denoised[y][x] = neighbors >= 1;
            }
        }
        int pixelCount = (box.right - box.left) * (box.bottom - box.top);
        int minimumComponent = (int) StrictMath.ceil(0.0025d * (double) pixelCount);
        if (minimumComponent < 1) minimumComponent = 1;
        boolean[][] retained = new boolean[denoised.length][denoised[0].length];
        boolean[][] seen = new boolean[denoised.length][denoised[0].length];
        int retainedCount = 0;
        for (int sy = 0; sy < denoised.length; sy++) {
            for (int sx = 0; sx < denoised[0].length; sx++) {
                if (!denoised[sy][sx] || seen[sy][sx]) continue;
                List<int[]> component = new ArrayList<>();
                ArrayDeque<int[]> queue = new ArrayDeque<>();
                queue.add(new int[] {sx, sy});
                seen[sy][sx] = true;
                while (!queue.isEmpty()) {
                    int[] point = queue.removeFirst();
                    component.add(point);
                    for (int dy = -1; dy <= 1; dy++) {
                        for (int dx = -1; dx <= 1; dx++) {
                            if (dx == 0 && dy == 0) continue;
                            int nx = point[0] + dx;
                            int ny = point[1] + dy;
                            if (ny >= 0 && ny < denoised.length
                                    && nx >= 0 && nx < denoised[0].length
                                    && denoised[ny][nx] && !seen[ny][nx]) {
                                seen[ny][nx] = true;
                                queue.addLast(new int[] {nx, ny});
                            }
                        }
                    }
                }
                if (component.size() >= minimumComponent) {
                    for (int[] point : component) {
                        retained[point[1]][point[0]] = true;
                        retainedCount++;
                    }
                }
            }
        }
        if (retainedCount < 8) {
            throw new IllegalArgumentException(
                    "character has fewer than eight retained glyph pixels");
        }
        List<PixelScore> ranked = new ArrayList<>();
        for (int localY = 0; localY < retained.length; localY++) {
            for (int localX = 0; localX < retained[0].length; localX++) {
                if (!retained[localY][localX]) continue;
                int x = box.left + localX;
                int y = box.top + localY;
                double[] actual = linearPixel(rgb, width, x, y, lookup);
                double[] background = clamp(
                        fitted(coefficients, design(x, y, xlo, xhi, ylo, yhi)));
                double d = distance(oklab(actual), oklab(background));
                double ratio = contrast(actual, background);
                ranked.add(new PixelScore(d, y, x, actual, background, ratio));
            }
        }
        ranked.sort(
                Comparator.comparingDouble(PixelScore::distance).reversed()
                        .thenComparingInt(PixelScore::y)
                        .thenComparingInt(PixelScore::x)
        );
        int coreCount = (int) StrictMath.ceil(0.10d * (double) retainedCount);
        if (coreCount < 5) coreCount = 5;
        List<PixelScore> selected = new ArrayList<>(ranked.subList(0, coreCount));
        double minimumRatio = selected.get(0).ratio;
        for (PixelScore score : selected) {
            if (score.ratio < minimumRatio) minimumRatio = score.ratio;
        }
        finite(minimumRatio);
        return new CharacterResult(
                box,
                ring.size(),
                retainedCount,
                coreCount,
                coefficients,
                selected,
                minimumRatio
        );
    }

    private static void writeCharacter(DataOutputStream output, CharacterResult result)
            throws Exception {
        output.writeInt(result.box.left);
        output.writeInt(result.box.top);
        output.writeInt(result.box.right);
        output.writeInt(result.box.bottom);
        output.writeInt(result.ringCount);
        output.writeInt(result.glyphCount);
        output.writeInt(result.coreCount);
        for (int channel = 0; channel < 3; channel++) {
            for (int coefficient = 0; coefficient < 4; coefficient++) {
                output.writeLong(raw(result.coefficients[channel][coefficient]));
            }
        }
        output.writeLong(raw(result.minimumRatio));
        output.writeBoolean(result.minimumRatio >= 4.5d);
        output.writeInt(result.selected.size());
        for (PixelScore score : result.selected) {
            output.writeInt(score.x);
            output.writeInt(score.y);
            for (double value : score.actual) output.writeLong(raw(value));
            for (double value : score.background) output.writeLong(raw(value));
            output.writeLong(raw(score.distance));
            output.writeLong(raw(score.ratio));
        }
    }

    private static boolean contains(Box box, int x, int y) {
        return x >= box.left && x < box.right && y >= box.top && y < box.bottom;
    }

    private static int channel(byte[] rgb, int width, int x, int y, int channel) {
        return rgb[((y * width + x) * 3) + channel] & 0xff;
    }

    private static double[] linearPixel(
            byte[] rgb, int width, int x, int y, double[] lookup) {
        return new double[] {
                lookup[channel(rgb, width, x, y, 0)],
                lookup[channel(rgb, width, x, y, 1)],
                lookup[channel(rgb, width, x, y, 2)]
        };
    }

    private static double srgb(int q) {
        if (q < 0 || q > 255) throw new IllegalArgumentException("invalid sRGB input");
        double c = ((double) q) / 255.0d;
        double result;
        if (c <= 0.04045d) {
            result = c / 12.92d;
        } else {
            double n = c + 0.055d;
            double b = n / 1.055d;
            result = StrictMath.pow(b, 2.4d);
        }
        return finite(result);
    }

    private static double[] design(
            int px, int py, int xlo, int xhi, int ylo, int yhi) {
        double xd = (double) (px - xlo);
        double xs = (double) (xhi - xlo);
        double x0 = 2.0d * xd;
        double x1 = x0 / xs;
        double x = x1 - 1.0d;
        double yd = (double) (py - ylo);
        double ys = (double) (yhi - ylo);
        double y0 = 2.0d * yd;
        double y1 = y0 / ys;
        double y = y1 - 1.0d;
        return new double[] {1.0d, finite(x), finite(y), finite(x * y)};
    }

    private static double[] fit(double[][] design, double[] values) {
        double[] weights = new double[values.length];
        for (int i = 0; i < weights.length; i++) weights[i] = 1.0d;
        double[] coefficients = solve(design, values, weights);
        for (int iteration = 0; iteration < 10; iteration++) {
            for (int i = 0; i < values.length; i++) {
                double x = design[i][1];
                double y = design[i][2];
                double residual = ((coefficients[0] + coefficients[1] * x)
                        + coefficients[2] * y) + coefficients[3] * (x * y);
                double ar = StrictMath.abs(values[i] - residual);
                finite(ar);
                weights[i] = ar <= HUBER_DELTA ? 1.0d : HUBER_DELTA / ar;
                finite(weights[i]);
            }
            coefficients = solve(design, values, weights);
        }
        return coefficients;
    }

    private static double[] solve(double[][] design, double[] values, double[] weights) {
        double[][] matrix = new double[4][4];
        double[] z = new double[4];
        for (int i = 0; i < design.length; i++) {
            for (int p = 0; p < 4; p++) {
                for (int q = 0; q < 4; q++) {
                    double t0 = weights[i] * design[i][p];
                    double t1 = t0 * design[i][q];
                    matrix[p][q] = finite(matrix[p][q] + t1);
                }
                double u0 = weights[i] * design[i][p];
                double u1 = u0 * values[i];
                z[p] = finite(z[p] + u1);
            }
        }
        for (int k = 0; k < 4; k++) {
            int pivotRow = k;
            double pivotAbs = StrictMath.abs(matrix[k][k]);
            for (int r = k + 1; r < 4; r++) {
                double candidate = StrictMath.abs(matrix[r][k]);
                if (candidate > pivotAbs) {
                    pivotRow = r;
                    pivotAbs = candidate;
                }
            }
            if (!(pivotAbs > 1.0e-12d)) {
                throw new IllegalArgumentException("singular weighted solve");
            }
            if (pivotRow != k) {
                double[] row = matrix[k];
                matrix[k] = matrix[pivotRow];
                matrix[pivotRow] = row;
                double value = z[k];
                z[k] = z[pivotRow];
                z[pivotRow] = value;
            }
            double pivot = matrix[k][k];
            for (int j = k; j < 4; j++) {
                matrix[k][j] = finite(matrix[k][j] / pivot);
            }
            z[k] = finite(z[k] / pivot);
            for (int r = k + 1; r < 4; r++) {
                double factor = matrix[r][k];
                for (int j = k; j < 4; j++) {
                    double product = factor * matrix[k][j];
                    matrix[r][j] = finite(matrix[r][j] - product);
                }
                double productZ = factor * z[k];
                z[r] = finite(z[r] - productZ);
            }
        }
        double[] coefficients = new double[4];
        for (int i = 3; i >= 0; i--) {
            double acc = z[i];
            for (int j = i + 1; j < 4; j++) {
                double product = matrix[i][j] * coefficients[j];
                acc = finite(acc - product);
            }
            coefficients[i] = finite(acc / matrix[i][i]);
        }
        return coefficients;
    }

    private static double[] fitted(double[][] coefficients, double[] row) {
        double[] output = new double[3];
        double x = row[1];
        double y = row[2];
        for (int channel = 0; channel < 3; channel++) {
            output[channel] = finite(
                    ((coefficients[channel][0] + coefficients[channel][1] * x)
                            + coefficients[channel][2] * y)
                            + coefficients[channel][3] * (x * y)
            );
        }
        return output;
    }

    private static double[] clamp(double[] values) {
        double[] output = new double[3];
        for (int i = 0; i < 3; i++) {
            double value = finite(values[i]);
            if (value < 0.0d) value = 0.0d;
            if (value > 1.0d) value = 1.0d;
            output[i] = value;
        }
        return output;
    }

    private static double[] oklab(double[] rgb) {
        double[] c = clamp(rgb);
        double l0 = 0.4122214708d * c[0];
        double l1 = 0.5363325363d * c[1];
        double l2 = l0 + l1;
        double l3 = 0.0514459929d * c[2];
        double l = l2 + l3;
        double m0 = 0.2119034982d * c[0];
        double m1 = 0.6806995451d * c[1];
        double m2 = m0 + m1;
        double m3 = 0.1073969566d * c[2];
        double m = m2 + m3;
        double s0 = 0.0883024619d * c[0];
        double s1 = 0.2817188376d * c[1];
        double s2 = s0 + s1;
        double s3 = 0.6299787005d * c[2];
        double s = s2 + s3;
        double lp = StrictMath.cbrt(finite(l));
        double mp = StrictMath.cbrt(finite(m));
        double sp = StrictMath.cbrt(finite(s));
        double upperL = (0.2104542553d * lp) + (0.7936177850d * mp);
        double resultL = upperL - (0.0040720468d * sp);
        double upperA = (1.9779984951d * lp) - (2.4285922050d * mp);
        double resultA = upperA + (0.4505937099d * sp);
        double upperB = (0.0259040371d * lp) + (0.7827717662d * mp);
        double resultB = upperB - (0.8086757660d * sp);
        return new double[] {finite(resultL), finite(resultA), finite(resultB)};
    }

    private static double distance(double[] first, double[] second) {
        double dl = first[0] - second[0];
        double da = first[1] - second[1];
        double db = first[2] - second[2];
        double q0 = dl * dl;
        double q1 = da * da;
        double q2 = db * db;
        double q3 = q0 + q1;
        double q4 = q3 + q2;
        return finite(StrictMath.sqrt(finite(q4)));
    }

    private static double contrast(double[] first, double[] second) {
        double first01 = (0.2126d * first[0]) + (0.7152d * first[1]);
        double firstL = finite(first01 + (0.0722d * first[2]));
        double second01 = (0.2126d * second[0]) + (0.7152d * second[1]);
        double secondL = finite(second01 + (0.0722d * second[2]));
        double high = firstL >= secondL ? firstL : secondL;
        double low = firstL <= secondL ? firstL : secondL;
        return finite((high + 0.05d) / (low + 0.05d));
    }

    private static long raw(double value) {
        finite(value);
        if (value == 0.0d) value = 0.0d;
        return Double.doubleToRawLongBits(value);
    }

    private static double finite(double value) {
        if (!Double.isFinite(value)) {
            throw new IllegalArgumentException("non-finite NumericReference value");
        }
        return value;
    }
}
