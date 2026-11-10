public class ArrayStr {
    public static void main(String[] args) {
        int n = args.length > 0 ? Integer.parseInt(args[0]) : 2_000_000;
        String[][] keep = new String[n][];
        long sink = 0;
        long t0 = System.nanoTime();
        for (int i = 0; i < n; i++) {
            String[] a = {"a", "bb"};
            sink += a[0].length() + a[1].length();
            keep[i] = a;
        }
        long d = System.nanoTime() - t0;
        System.out.printf("java-arrstr n=%d ms=%.1f ns_per_op=%.1f sink=%d%n",
                n, d / 1e6, d / (double) n, sink);
    }
}
