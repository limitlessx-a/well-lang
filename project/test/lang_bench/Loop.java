public class Loop {
    public static void main(String[] args) {
        long n = args.length > 0 ? Long.parseLong(args[0]) : 100_000_000L;
        long acc = 0;
        long t0 = System.nanoTime();
        for (long i = 0; i < n; i++) acc += i;
        long d = System.nanoTime() - t0;
        System.out.printf("java n=%d ms=%.1f ns_per_iter=%.3f acc=%d%n",
                n, d / 1e6, d / (double) n, acc);
    }
}
