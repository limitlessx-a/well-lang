-module(loop).
-export([main/1]).

main([Arg]) ->
    N = case Arg of
            A when is_atom(A) -> list_to_integer(atom_to_list(A));
            A when is_list(A) -> list_to_integer(A)
        end,
    {Micros, Acc} = timer:tc(fun() -> loop(0, N, 0) end),
    Ms = Micros / 1000.0,
    io:format("erlang n=~p ms=~.1f ns_per_iter=~.3f acc=~p~n",
              [N, Ms, Ms * 1000.0 / N, Acc]).

loop(I, N, Acc) when I < N ->
    loop(I + 1, N, Acc + I);
loop(_, _, Acc) ->
    Acc.
