import java.util.HashMap;
import java.util.Map;

public class MapStack {
    static volatile Object last;
    public static void main(String[] args) {
        int n = args.length > 0 ? Integer.parseInt(args[0]) : 2_000_000;
        long sink = 0;
        long t0 = System.nanoTime();
        for (int i = 0; i < n; i++) {
            Map<String, Integer> m = new HashMap<>();
            m.put("a", 1);
            m.put("b", 2);
            sink += m.get("a") + m.get("b");
            last = m;
        }
        long d = System.nanoTime() - t0;
        System.out.printf("java-mapstack n=%d ms=%.1f ns_per_op=%.1f sink=%d%n",
                n, d / 1e6, d / (double) n, sink);
    }
}
