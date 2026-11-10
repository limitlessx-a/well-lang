public class ScalarBench {
    static final class Point { int x; int y; long z; Point(int x, int y, long z) { this.x=x; this.y=y; this.z=z; } }
    public static void main(String[] args) {
        int n = args.length > 0 ? Integer.parseInt(args[0]) : 2_000_000;
        Point[] keep = new Point[n];
        long t0 = System.nanoTime();
        long sink = 0;
        for (int i = 0; i < n; i++) {
            Point p = new Point(1, 2, i);
            sink += p.x + p.y + p.z;
            keep[i] = p;
        }
        long d = System.nanoTime() - t0;
        System.out.printf("java-scalar n=%d ms=%.1f ns_per_op=%.1f sink=%d%n",
                n, d / 1e6, d / (double) n, sink);
    }
}
