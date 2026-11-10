public class ArrayStack {
    public static void main(String[] args) {
        int n = args.length > 0 ? Integer.parseInt(args[0]) : 2_000_000;
        int[][] keep = new int[n][];
        long sink = 0;
        long t0 = System.nanoTime();
        for (int i = 0; i < n; i++) {
            int[] a = {1, 2, 3};
            sink += a[0] + a[1] + a[2];
            keep[i] = a;
        }
        long d = System.nanoTime() - t0;
        System.out.printf("java-array n=%d ms=%.1f ns_per_op=%.1f sink=%d%n",
                n, d / 1e6, d / (double) n, sink);
    }
}
