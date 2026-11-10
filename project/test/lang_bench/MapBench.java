import java.util.HashMap;

public class MapBench {
    static volatile Object last;
    public static void main(String[] args) {
        int n = args.length > 0 ? Integer.parseInt(args[0]) : 2_000_000;
        long sink = 0;
        long t0 = System.nanoTime();
        for (int i = 0; i < n; i++) {
            HashMap<String, Long> m = new HashMap<>();
            m.put("k", (long) i);
            sink += m.get("k");
            last = m;
        }
        long d = System.nanoTime() - t0;
        System.out.printf("java-map n=%d ms=%.1f ns_per_op=%.1f sink=%d%n",
                n, d / 1e6, d / (double) n, sink);
    }
}
