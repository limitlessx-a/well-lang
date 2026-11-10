-module(arr).
-export([main/1]).

main([Arg]) ->
    N = case Arg of
            A when is_atom(A) -> list_to_integer(atom_to_list(A));
            A when is_list(A) -> list_to_integer(A)
        end,
    T = list_to_tuple(lists:seq(0, 1023)),
    {Micros, Acc} = timer:tc(fun() -> loop(0, N, T, 0) end),
    Ms = Micros / 1000.0,
    io:format("erlang-arr n=~p ms=~.1f ns_per_iter=~.3f acc=~p~n",
              [N, Ms, Ms * 1000000.0 / N, Acc]).

loop(I, N, _T, Acc) when I >= N -> Acc;
loop(I, N, T, Acc) ->
    loop(I + 1, N, T, Acc + element((I band 1023) + 1, T)).
