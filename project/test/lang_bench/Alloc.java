public class Alloc {
    public static void main(String[] args) {
        int n = args.length > 0 ? Integer.parseInt(args[0]) : 2_000_000;
        Object[] keep = new Object[n];
        long t0 = System.nanoTime();
        for (int i = 0; i < n; i++) keep[i] = new byte[64];
        long d = System.nanoTime() - t0;
        System.out.printf("java alloc n=%d ms=%.1f ns_per_op=%.1f%n",
                n, d / 1e6, d / (double) n);
        keep = null;
        System.gc();
    }
}
