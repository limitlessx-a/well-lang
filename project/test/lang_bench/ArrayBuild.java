import java.util.ArrayList;

public class ArrayBuild {
    static volatile Object last;
    public static void main(String[] args) {
        int n = args.length > 0 ? Integer.parseInt(args[0]) : 2_000_000;
        long sink = 0;
        long t0 = System.nanoTime();
        for (int i = 0; i < n; i++) {
            ArrayList<Integer> a = new ArrayList<>();
            a.add(1);
            a.add(2);
            sink += a.get(0) + a.get(1);
            last = a;
        }
        long d = System.nanoTime() - t0;
        System.out.printf("java-arrbuild n=%d ms=%.1f ns_per_op=%.1f sink=%d%n",
                n, d / 1e6, d / (double) n, sink);
    }
}
