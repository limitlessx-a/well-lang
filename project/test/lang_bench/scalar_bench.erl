-module(scalar_bench).
-export([main/1]).

main([Arg]) ->
    N = case Arg of
            A when is_atom(A) -> list_to_integer(atom_to_list(A));
            A when is_list(A) -> list_to_integer(A)
        end,
    {Micros, Sink} = timer:tc(fun() -> loop(0, N, 0) end),
    Ms = Micros / 1000.0,
    io:format("erlang-scalar n=~p ms=~.1f ns_per_op=~.1f sink=~p~n",
              [N, Ms, Ms * 1000000.0 / N, Sink]).

loop(I, N, Sink) when I >= N -> Sink;
loop(I, N, Sink) ->
    P = #{x => 1, y => 2, z => I},
    loop(I + 1, N, Sink + maps:get(x, P) + maps:get(y, P) + maps:get(z, P)).
