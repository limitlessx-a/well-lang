public class Arr {
    public static void main(String[] args) {
        int n = args.length > 0 ? Integer.parseInt(args[0]) : 20_000_000;
        long[] a = new long[1024];
        for (int i = 0; i < 1024; i++) a[i] = i;
        long acc = 0;
        long t0 = System.nanoTime();
        for (int i = 0; i < n; i++) acc += a[i & 1023];
        long d = System.nanoTime() - t0;
        System.out.printf("java-arr n=%d ms=%.1f ns_per_iter=%.3f acc=%d%n",
                n, d / 1e6, d / (double) n, acc);
    }
}
