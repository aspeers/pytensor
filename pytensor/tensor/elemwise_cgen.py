from pytensor.configdefaults import config


def make_declare(loop_orders, dtypes, sub):
    """
    Produce code to declare all necessary variables.

    """
    decl = ""
    for i, (loop_order, dtype) in enumerate(zip(loop_orders, dtypes)):
        var = sub[f"lv{int(i)}"]  # input name corresponding to ith loop variable
        # we declare an iteration variable
        # and an integer for the number of dimensions
        decl += f"""
        {dtype}* {var}_iter;
        """
        for j, value in enumerate(loop_order):
            if value != "x":
                # If the dimension is not broadcasted, we declare
                # the number of elements in that dimension,
                # the stride in that dimension,
                # and the jump from an iteration to the next
                decl += f"""
                npy_intp {var}_n{int(value)};
                ssize_t {var}_stride{int(value)};
                int {var}_jump{int(value)}_{int(j)};
                """

            else:
                # if the dimension is broadcasted, we only need
                # the jump (arbitrary length and stride = 0)
                decl += f"""
                int {var}_jump{value}_{int(j)};
                """

    return decl


def make_checks(loop_orders, dtypes, sub):
    init = ""
    for i, (loop_order, dtype) in enumerate(zip(loop_orders, dtypes)):
        var = f"%(lv{int(i)})s"
        # List of dimensions of var that are not broadcasted
        nonx = [x for x in loop_order if x != "x"]
        if nonx:
            # If there are dimensions that are not broadcasted
            # this is a check that the number of dimensions of the
            # tensor is as expected.
            min_nd = max(nonx) + 1
            init += f"""
            if (PyArray_NDIM({var}) < {min_nd}) {{
                PyErr_SetString(PyExc_ValueError, "Not enough dimensions on input.");
                %(fail)s
            }}
            """

        # In loop j, adjust represents the difference of values of the
        # data pointer between the beginning and the end of the
        # execution of loop j+1 (the loop inside the current one). It
        # is equal to the stride in loop j+1 times the length of loop
        # j+1, or 0 for the inner-most loop.
        adjust = "0"

        # We go from the inner loop to the outer loop
        for j, index in reversed(list(enumerate(loop_order))):
            if index != "x":
                # Initialize the variables associated to the jth loop
                # jump = stride - adjust
                jump = f"({var}_stride{index}) - ({adjust})"
                init += f"""
                {var}_n{index} = PyArray_DIMS({var})[{index}];
                {var}_stride{index} = PyArray_STRIDES({var})[{index}] / sizeof({dtype});
                {var}_jump{index}_{j} = {jump};
                """
                adjust = f"{var}_n{index}*{var}_stride{index}"
            else:
                jump = f"-({adjust})"
                init += f"""
                {var}_jump{index}_{j} = {jump};
                """
                adjust = "0"
    check = ""

    # This loop builds multiple if conditions to verify that the
    # dimensions of the inputs match, and the first one that is true
    # raises an informative error message

    runtime_broadcast_error_msg = (
        "Runtime broadcasting not allowed. "
        "One input had a distinct dimension length of 1, but was not marked as broadcastable: "
        "(input[%%i].shape[%%i] = %%lld, input[%%i].shape[%%i] = %%lld). "
        "If broadcasting was intended, use `specify_broadcastable` on the relevant input."
    )

    for matches in zip(*loop_orders):
        to_compare = [(j, x) for j, x in enumerate(matches) if x != "x"]

        # elements of to_compare are pairs ( input_variable_idx, input_variable_dim_idx )
        if len(to_compare) < 2:
            continue

        j0, x0 = to_compare[0]
        for j, x in to_compare[1:]:
            check += f"""
            if (%(lv{j0})s_n{x0} != %(lv{j})s_n{x})
            {{
                if (%(lv{j0})s_n{x0} == 1 || %(lv{j})s_n{x} == 1)
                {{
                    PyErr_Format(PyExc_ValueError, "{runtime_broadcast_error_msg}",
                   {j0},
                   {x0},
                   (long long int) %(lv{j0})s_n{x0},
                   {j},
                   {x},
                   (long long int) %(lv{j})s_n{x}
                    );
                }} else {{
                    PyErr_Format(PyExc_ValueError, "Input dimension mismatch: (input[%%i].shape[%%i] = %%lld, input[%%i].shape[%%i] = %%lld)",
                       {j0},
                       {x0},
                       (long long int) %(lv{j0})s_n{x0},
                       {j},
                       {x},
                       (long long int) %(lv{j})s_n{x}
                    );
                }}
                %(fail)s
            }}
        """

    return init % sub + check % sub


def compute_output_dims_lengths(array_name: str, loop_orders, sub) -> str:
    """Create c_code to compute the output dimensions of an Elemwise operation.

    The code returned by this function populates the array `array_name`, but does not
    initialize it.

    Note: We could specialize C code even further with the known static output shapes
    """
    dims_c_code = ""
    for i, candidates in enumerate(zip(*loop_orders)):
        # Borrow the length of the first non-broadcastable input dimension
        for j, candidate in enumerate(candidates):
            if candidate != "x":
                var = sub[f"lv{int(j)}"]
                dims_c_code += f"{array_name}[{i}] = {var}_n{candidate};\n"
                break
        # If none is non-broadcastable, the output dimension has a length of 1
        else:  # no-break
            dims_c_code += f"{array_name}[{i}] = 1;\n"

    return dims_c_code


def make_alloc(loop_orders, dtype, sub, fortran="0"):
    """Generate C code to allocate outputs.

    Parameters
    ----------
    fortran : str
        A string included in the generated code. If it
        evaluate to non-zero, an ndarray in fortran order will be
        created, otherwise it will be c order.

    """
    type = dtype.upper()
    if type.startswith("PYTENSOR_COMPLEX"):
        type = type.replace("PYTENSOR_COMPLEX", "NPY_COMPLEX")
    nd = len(loop_orders[0])
    init_dims = compute_output_dims_lengths("dims", loop_orders, sub)

    # TODO: it would be interesting to allocate the output in such a
    # way that its contiguous dimensions match one of the input's
    # contiguous dimensions, or the dimension with the smallest
    # stride. Right now, it is allocated to be C_CONTIGUOUS.
    return """
    {
        npy_intp dims[%(nd)s];
        //npy_intp* dims = (npy_intp*)malloc(%(nd)s * sizeof(npy_intp));
        %(init_dims)s
        if (!%(olv)s) {
            %(olv)s = (PyArrayObject*)PyArray_EMPTY(%(nd)s, dims,
                                                    %(type)s,
                                                    %(fortran)s);
        }
        else {
            PyArray_Dims new_dims;
            new_dims.len = %(nd)s;
            new_dims.ptr = dims;
            PyObject* success = PyArray_Resize(%(olv)s, &new_dims, 0, NPY_CORDER);
            if (!success) {
                // If we can't resize the ndarray we have we can allocate a new one.
                PyErr_Clear();
                Py_XDECREF(%(olv)s);
                %(olv)s = (PyArrayObject*)PyArray_EMPTY(%(nd)s, dims, %(type)s, 0);
            } else {
                Py_DECREF(success);
            }
        }
        if (!%(olv)s) {
            %(fail)s
        }
    }
    """ % dict(
        locals(), **sub
    )


def make_loop(loop_orders, dtypes, loop_tasks, sub, openmp=None):
    """
    Make a nested loop over several arrays and associate specific code
    to each level of nesting.

    Parameters
    ----------
    loop_orders : list of N tuples of length M
        Each value of each tuple can be either the index of a dimension to
        loop over or the letter 'x' which means there is no looping to be done
        over that variable at that point (in other words we broadcast
        over that dimension). If an entry is an integer, it will become
        an alias of the entry of that rank.
    loop_tasks : list of M+1 pieces of code
        The ith loop_task is a pair of strings, the first
        string is code to be executed before the ith loop starts, the second
        one contains code to be executed just before going to the next element
        of the ith dimension.
        The last element if loop_tasks is a single string, containing code
        to be executed at the very end.
    sub : dictionary
        Maps 'lv#' to a suitable variable name.
        The 'lvi' variable corresponds to the ith element of loop_orders.

    """

    def loop_over(preloop, code, indices, i):
        iterv = f"ITER_{int(i)}"
        update = ""
        suitable_n = "1"
        for j, index in enumerate(indices):
            var = sub[f"lv{int(j)}"]
            dtype = dtypes[j]
            update += f"{dtype} &{var}_i = * ( {var}_iter + {iterv} * {var}_jump{index}_{i} );\n"

            if index != "x":
                suitable_n = f"{var}_n{index}"
        if openmp:
            openmp_elemwise_minsize = config.openmp_elemwise_minsize
            forloop = f"""#pragma omp parallel for if( {suitable_n} >={openmp_elemwise_minsize})\n"""
        else:
            forloop = ""
        forloop += f"""for (int {iterv} = 0; {iterv}<{suitable_n}; {iterv}++)"""
        return f"""
        {preloop}
        {forloop} {{
            {update}
            {code}
        }}
        """

    preloops = {}
    for i, (loop_order, dtype) in enumerate(zip(loop_orders, dtypes)):
        for j, index in enumerate(loop_order):
            if index != "x":
                preloops.setdefault(j, "")
                preloops[j] += (
                    f"%(lv{i})s_iter = ({dtype}*)(PyArray_DATA(%(lv{i})s));\n"
                ) % sub
                break
        else:  # all broadcastable
            preloops.setdefault(0, "")
            preloops[0] += (
                f"%(lv{i})s_iter = ({dtype}*)(PyArray_DATA(%(lv{i})s));\n"
            ) % sub

    s = ""

    for i, (pre_task, task), indices in reversed(
        list(zip(range(len(loop_tasks) - 1), loop_tasks, list(zip(*loop_orders))))
    ):
        s = loop_over(preloops.get(i, "") + pre_task, s + task, indices, i)

    s += loop_tasks[-1]
    return f"{{{s}}}"


def make_reordered_loop(
    init_loop_orders, olv_index, dtypes, inner_task, sub, openmp=None
):
    """A bit like make_loop, but when only the inner-most loop executes code.

    All the loops will be reordered so that the loops over the output tensor
    are executed with memory access as contiguous as possible.
    For instance, if the output tensor is c_contiguous, the inner-most loop
    will be on its rows; if it's f_contiguous, it will be on its columns.

    The output tensor's index among the loop variables is indicated by olv_index.

    """

    # Number of variables
    nvars = len(init_loop_orders)
    # Number of loops (dimensionality of the variables)
    nnested = len(init_loop_orders[0])

    # This is the var from which we'll get the loop order
    ovar = sub[f"lv{int(olv_index)}"]

    # The loops are ordered by (decreasing) absolute values of ovar's strides.
    # The first element of each pair is the absolute value of the stride
    # The second element correspond to the index in the initial loop order
    order_loops = f"""
    std::vector< std::pair<int, int> > {ovar}_loops({int(nnested)});
    std::vector< std::pair<int, int> >::iterator {ovar}_loops_it = {ovar}_loops.begin();
    """

    # Fill the loop vector with the appropriate <stride, index> pairs
    for i, index in enumerate(init_loop_orders[olv_index]):
        if index != "x":
            order_loops += f"""
            {ovar}_loops_it->first = abs(PyArray_STRIDES({ovar})[{int(index)}]);
            """
        else:
            # Stride is 0 when dimension is broadcastable
            order_loops += f"""
            {ovar}_loops_it->first = 0;
            """

        order_loops += f"""
        {ovar}_loops_it->second = {int(i)};
        ++{ovar}_loops_it;
        """

    # We sort in decreasing order so that the outermost loop (loop 0)
    # has the largest stride, and the innermost loop (nnested - 1) has
    # the smallest stride.
    order_loops += f"""
    // rbegin and rend are reversed iterators, so this sorts in decreasing order
    std::sort({ovar}_loops.rbegin(), {ovar}_loops.rend());
    """

    # Get the (sorted) total number of iterations of each loop
    declare_totals = f"int init_totals[{nnested}];\n"
    declare_totals += compute_output_dims_lengths("init_totals", init_loop_orders, sub)

    # Sort totals to match the new order that was computed by sorting
    # the loop vector. One integer variable per loop is declared.
    declare_totals += f"""
    {ovar}_loops_it = {ovar}_loops.begin();
    """

    for i in range(nnested):
        declare_totals += f"""
        int TOTAL_{int(i)} = init_totals[{ovar}_loops_it->second];
        ++{ovar}_loops_it;
        """

    # Get sorted strides
    # Get strides in the initial order
    def get_loop_strides(loop_order, i):
        """
        Returns a list containing a C expression representing the
        stride for each dimension of the ith variable, in the
        specified loop_order.

        """
        var = sub[f"lv{int(i)}"]
        r = []
        for index in loop_order:
            # Note: the stride variable is not declared for broadcasted variables
            if index != "x":
                r.append(f"{var}_stride{index}")
            else:
                r.append("0")
        return r

    # We declare the initial strides as a 2D array, nvars x nnested
    strides = ", \n".join(
        ", ".join(get_loop_strides(lo, i))
        for i, lo in enumerate(init_loop_orders)
        if len(lo) > 0
    )

    declare_strides = f"""
    int init_strides[{int(nvars)}][{int(nnested)}] = {{
        {strides}
    }};"""

    # Declare (sorted) stride and for each variable
    # we iterate from innermost loop to outermost loop
    declare_strides += f"""
    std::vector< std::pair<int, int> >::reverse_iterator {ovar}_loops_rit;
    """

    for i in range(nvars):
        var = sub[f"lv{int(i)}"]
        declare_strides += f"""
        {ovar}_loops_rit = {ovar}_loops.rbegin();"""
        for j in reversed(range(nnested)):
            declare_strides += f"""
            int {var}_stride_l{int(j)} = init_strides[{int(i)}][{ovar}_loops_rit->second];
            ++{ovar}_loops_rit;
            """

    declare_iter = ""
    for i, dtype in enumerate(dtypes):
        var = sub[f"lv{int(i)}"]
        declare_iter += f"{var}_iter = ({dtype}*)(PyArray_DATA({var}));\n"

    pointer_update = ""
    for j, dtype in enumerate(dtypes):
        var = sub[f"lv{int(j)}"]
        pointer_update += f"{dtype} &{var}_i = * ( {var}_iter"
        for i in reversed(range(nnested)):
            iterv = f"ITER_{int(i)}"
            pointer_update += f"+{var}_stride_l{int(i)}*{iterv}"
        pointer_update += ");\n"

    loop = inner_task
    for i in reversed(range(nnested)):
        iterv = f"ITER_{int(i)}"
        total = f"TOTAL_{int(i)}"
        update = ""
        forloop = ""
        # The pointers are defined only in the most inner loop
        if i == nnested - 1:
            update = pointer_update
        if i == 0:
            if openmp:
                openmp_elemwise_minsize = config.openmp_elemwise_minsize
                forloop += f"""#pragma omp parallel for if( {total} >={openmp_elemwise_minsize})\n"""
        forloop += f"for(int {iterv} = 0; {iterv}<{total}; {iterv}++)"

        loop = f"""
        {forloop}
        {{ // begin loop {int(i)}
            {update}
            {loop}
        }} // end loop {int(i)}
        """

    return "\n".join(
        ["{", order_loops, declare_totals, declare_strides, declare_iter, loop, "}\n"]
    )


# print make_declare(((0, 1, 2, 3), ('x', 1, 0, 3), ('x', 'x', 'x', 0)),
#                    ('double', 'int', 'float'),
#                    dict(lv0='x', lv1='y', lv2='z', fail="FAIL;"))

# print make_checks(((0, 1, 2, 3), ('x', 1, 0, 3), ('x', 'x', 'x', 0)),
#                   ('double', 'int', 'float'),
#                   dict(lv0='x', lv1='y', lv2='z', fail="FAIL;"))

# print make_alloc(((0, 1, 2, 3), ('x', 1, 0, 3), ('x', 'x', 'x', 0)),
#                  'double',
#                  dict(olv='out', lv0='x', lv1='y', lv2='z', fail="FAIL;"))

# print make_loop(((0, 1, 2, 3), ('x', 1, 0, 3), ('x', 'x', 'x', 0)),
#                 ('double', 'int', 'float'),
#                 (("C00;", "C%01;"), ("C10;", "C11;"), ("C20;", "C21;"), ("C30;", "C31;"),"C4;"),
#                 dict(lv0='x', lv1='y', lv2='z', fail="FAIL;"))

# print make_loop(((0, 1, 2, 3), (3, 'x', 0, 'x'), (0, 'x', 'x', 'x')),
#                 ('double', 'int', 'float'),
#                 (("C00;", "C01;"), ("C10;", "C11;"), ("C20;", "C21;"), ("C30;", "C31;"),"C4;"),
#                 dict(lv0='x', lv1='y', lv2='z', fail="FAIL;"))


##################
#   DimShuffle   #
##################

#################
#   Broadcast   #
#################


################
#   CAReduce   #
################


def make_loop_careduce(loop_orders, dtypes, loop_tasks, sub):
    """
    Make a nested loop over several arrays and associate specific code
    to each level of nesting.

    Parameters
    ----------
    loop_orders : list of N tuples of length M
        Each value of each tuple can be either the index of a dimension to
        loop over or the letter 'x' which means there is no looping to be done
        over that variable at that point (in other words we broadcast
        over that dimension). If an entry is an integer, it will become
        an alias of the entry of that rank.
    loop_tasks : list of M+1 pieces of code
        The ith loop_task is a pair of strings, the first
        string is code to be executed before the ith loop starts, the second
        one contains code to be executed just before going to the next element
        of the ith dimension.
        The last element if loop_tasks is a single string, containing code
        to be executed at the very end.
    sub: dictionary
        Maps 'lv#' to a suitable variable name.
        The 'lvi' variable corresponds to the ith element of loop_orders.

    """

    def loop_over(preloop, code, indices, i):
        iterv = f"ITER_{int(i)}"
        update = ""
        suitable_n = "1"
        for j, index in enumerate(indices):
            var = sub[f"lv{int(j)}"]
            update += f"{var}_iter += {var}_jump{index}_{i};\n"
            if index != "x":
                suitable_n = f"{var}_n{index}"
        return f"""
        {preloop}
        for (int {iterv} = {suitable_n}; {iterv}; {iterv}--) {{
            {code}
            {update}
        }}
        """

    preloops = {}
    for i, (loop_order, dtype) in enumerate(zip(loop_orders, dtypes)):
        for j, index in enumerate(loop_order):
            if index != "x":
                preloops.setdefault(j, "")
                preloops[j] += (
                    f"%(lv{i})s_iter = ({dtype}*)(PyArray_DATA(%(lv{i})s));\n"
                ) % sub
                break
        else:  # all broadcastable
            preloops.setdefault(0, "")
            preloops[0] += (
                f"%(lv{i})s_iter = ({dtype}*)(PyArray_DATA(%(lv{i})s));\n"
            ) % sub

    if len(loop_tasks) == 1:
        s = preloops.get(0, "")
    else:
        s = ""
        for i, (pre_task, task), indices in reversed(
            list(zip(range(len(loop_tasks) - 1), loop_tasks, list(zip(*loop_orders))))
        ):
            s = loop_over(preloops.get(i, "") + pre_task, s + task, indices, i)

    s += loop_tasks[-1]
    return f"{{{s}}}"
