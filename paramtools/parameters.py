import copy
import os
import json
import itertools
import warnings
from collections import OrderedDict, defaultdict
from functools import partial, reduce
from typing import Optional, Dict, List, Any

import numpy as np
from marshmallow import ValidationError as MarshmallowValidationError

from paramtools import utils
from paramtools.schema import ParamToolsSchema
from paramtools.schema_factory import SchemaFactory
from paramtools.select import select_eq, select_gt_ix, select_gt
from paramtools.tree import Tree
from paramtools.typing import ValueObject
from paramtools.exceptions import (
    ParamToolsError,
    SparseValueObjectsException,
    ValidationError,
    InconsistentLabelsException,
    collision_list,
    ParameterNameCollisionException,
)


class Parameters:
    defaults = None
    field_map: Dict = {}
    array_first: bool = False
    label_to_extend: str = None
    uses_extend_func: bool = False
    index_rates: Dict = {}

    def __init__(
        self,
        initial_state: Optional[dict] = None,
        array_first: Optional[bool] = False,
        label_to_extend: str = None,
        uses_extend_func: bool = False,
        index_rates: Optional[dict] = None,
    ):
        schemafactory = SchemaFactory(self.defaults, self.field_map)
        (
            self._defaults_schema,
            self._validator_schema,
            self._schema,
            self._data,
        ) = schemafactory.schemas()
        self.label_validators = schemafactory.label_validators
        self._stateless_label_grid = OrderedDict(
            [(name, v.grid()) for name, v in self.label_validators.items()]
        )
        self.label_grid = copy.deepcopy(self._stateless_label_grid)
        self._validator_schema.context["spec"] = self
        self._warnings = {}
        self._errors = {}
        self._state = initial_state or {}
        self._search_trees = {}
        self.index_rates = index_rates or self.index_rates

        # set operators in order of importance:
        # __init__ arg: most important
        # class attribute: middle importance
        # schema action: least important
        # default value if three above are not specified.
        ops = [
            ("array_first", array_first, False),
            ("label_to_extend", label_to_extend, None),
            ("uses_extend_func", uses_extend_func, False),
        ]
        schema_ops = self._schema.get("operators", {})
        for name, init_value, default in ops:
            user_vals = [init_value, getattr(self, name), schema_ops.get(name)]
            for value in user_vals:
                if value != default and value is not None:
                    setattr(self, name, value)
                    break

        if self.label_to_extend:
            prev_array_first = self.array_first
            self.array_first = False
            self.set_state()
            self.extend()
            if prev_array_first:
                self.array_first = True
                self.set_state()
        else:
            self.set_state()

        if "operators" not in self._schema:
            self._schema["operators"] = {}
        self._schema["operators"].update(self.operators)

    def set_state(self, **labels):
        """
        Sets state for the Parameters instance. The state, label_grid, and
        parameter attributes are all updated with the new state.

        Raises:
            ValidationError if the labels kwargs contain labels that are not
                specified in schema.json or if the label values fail the
                validator set for the corresponding label in schema.json.
        """

        self._set_state(**labels)

    def clear_state(self):
        """
        Reset the state of the Parameters instance.
        """
        self._state = {}
        self.label_grid = copy.deepcopy(self._stateless_label_grid)
        self.set_state()

    def view_state(self):
        """
        Access the label state of the ``Parameters`` instance.
        """
        return self._state

    def read_params(self, params_or_path):
        if isinstance(params_or_path, str) and os.path.exists(params_or_path):
            params = utils.read_json(params_or_path)
        elif isinstance(params_or_path, str):
            params = json.loads(params_or_path)
        elif isinstance(params_or_path, dict):
            params = params_or_path
        else:
            raise ValueError("params_or_path is not dict or file path")
        return params

    def adjust(
        self,
        params_or_path,
        ignore_warnings=False,
        raise_errors=True,
        extend_adj=True,
    ):
        """
        Deserialize and validate parameter adjustments. `params_or_path`
        can be a file path or a `dict` that has not been fully deserialized.
        The adjusted values replace the current values stored in the
        corresponding parameter attributes. This simply calls a private
        method `_adjust` to do the upate. Creating this layer on top of
        `_adjust` makes it easy to subclass Parameters and implement custom
        `adjust` methods.

        Returns: parsed, validated parameters.

        Raises:
            marshmallow.exceptions.ValidationError if data is not valid.

            ParameterUpdateException if label values do not match at
                least one existing value item's corresponding label values.
        """
        return self._adjust(
            params_or_path,
            ignore_warnings=ignore_warnings,
            raise_errors=raise_errors,
            extend_adj=extend_adj,
        )

    def _adjust(
        self,
        params_or_path,
        ignore_warnings=False,
        raise_errors=True,
        extend_adj=True,
    ):
        """
        Internal method for performing adjustments.
        """
        params = self.read_params(params_or_path)

        # Validate user adjustments.
        parsed_params = {}
        try:
            parsed_params = self._validator_schema.load(
                params, ignore_warnings
            )
        except MarshmallowValidationError as ve:
            self._parse_validation_messages(ve.messages, params)

        if not self._errors:
            if self.label_to_extend is not None and extend_adj:
                extend_grid = self._stateless_label_grid[self.label_to_extend]
                to_delete = defaultdict(list)
                backup = {}
                for param, vos in parsed_params.items():
                    for vo in utils.grid_sort(
                        vos, self.label_to_extend, extend_grid
                    ):
                        if self.label_to_extend in vo:
                            if (
                                vo[self.label_to_extend]
                                not in self.label_grid[self.label_to_extend]
                            ):
                                msg = (
                                    f"{param}[{self.label_to_extend}={vo[self.label_to_extend]}] "
                                    f"is not active in the current state: "
                                    f"{self.label_to_extend}= "
                                    f"{self.label_grid[self.label_to_extend]}."
                                )
                                warnings.warn(msg)
                            gt = select_gt_ix(
                                self._data[param]["value"],
                                True,
                                {
                                    self.label_to_extend: vo[
                                        self.label_to_extend
                                    ]
                                },
                                extend_grid,
                                tree=self._search_trees.get(param),
                            )
                            eq = select_eq(
                                gt,
                                True,
                                utils.filter_labels(
                                    vo, drop=[self.label_to_extend, "value"]
                                ),
                            )
                            to_delete[param] += [
                                dict(td, **{"value": None}) for td in eq
                            ]
                    # make copy of value objects since they
                    # are about to be modified
                    backup[param] = copy.deepcopy(self._data[param]["value"])
                try:
                    array_first = self.array_first
                    self.array_first = False

                    # delete params that will be overwritten out by extend.
                    self._adjust(
                        to_delete,
                        extend_adj=False,
                        raise_errors=True,
                        ignore_warnings=ignore_warnings,
                    )

                    # set user adjustments.
                    self._adjust(
                        parsed_params,
                        extend_adj=False,
                        raise_errors=True,
                        ignore_warnings=ignore_warnings,
                    )
                    self.extend(
                        params=parsed_params.keys(),
                        ignore_warnings=ignore_warnings,
                        raise_errors=True,
                    )
                except ValidationError:
                    for param in backup:
                        self._data[param]["value"] = backup[param]
                finally:
                    self.array_first = array_first
            else:
                for param, value in parsed_params.items():
                    self._update_param(param, value)

        self._validator_schema.context["spec"] = self

        has_errors = bool(self._errors.get("messages"))
        has_warnings = bool(self._warnings.get("messages"))
        # throw error if raise_errors is True or ignore_warnings is False
        if (raise_errors and has_errors) or (
            not ignore_warnings and has_warnings
        ):
            raise self.validation_error

        # Update attrs for params that were adjusted.
        self._set_state(params=parsed_params.keys())

        return parsed_params

    @property
    def errors(self):
        if not self._errors:
            return {}
        return {
            param: utils.ravel(messages)
            for param, messages in self._errors["messages"].items()
        }

    @property
    def warnings(self):
        if not self._warnings:
            return {}
        return {
            param: utils.ravel(messages)
            for param, messages in self._warnings["messages"].items()
        }

    @property
    def validation_error(self):
        messages = {
            "errors": self._errors.get("messages", {}),
            "warnings": self._warnings.get("messages", {}),
        }
        labels = {
            "errors": self._errors.get("labels", {}),
            "warnings": self._warnings.get("labels", {}),
        }
        return ValidationError(messages=messages, labels=labels)

    @property
    def operators(self):
        return {
            "array_first": self.array_first,
            "label_to_extend": self.label_to_extend,
            "uses_extend_func": self.uses_extend_func,
        }

    def dump(self, sort_values=True):
        """
        Dump a representation of this instance to JSON. This makes it
        possible to load this instance's data after sending the data
        across the wire or from another programming language. The
        dumped values will be queried using this instance's state.
        """
        spec = self.specification(
            meta_data=True,
            include_empty=True,
            serializable=True,
            sort_values=sort_values,
        )
        schema = ParamToolsSchema().dump(self._schema)
        return {**spec, **{"schema": schema}}

    def specification(
        self,
        use_state=True,
        meta_data=False,
        include_empty=False,
        serializable=False,
        sort_values=False,
        **labels,
    ):
        """
        Query value(s) of all parameters along labels specified in
        `labels`.

        Parameters:
            - use_state: If true, use the instance's state for the select operation.
            - meta_data: If true, include information like the parameter
                description and title.
            - include_empty: If true, include parameters that do not meet the label query.
            - serializable: If true, return data that is compatible with `json.dumps`.

        Returns: serialized data of shape
            {"param_name": [{"value": val, "label0": ..., }], ...}
        """
        if use_state:
            labels.update(self._state)

        all_params = OrderedDict()
        for param in self._validator_schema.fields:
            result = self.select_eq(param, False, **labels)
            if sort_values and result:
                self.sort_values(data={param: result}, has_meta_data=False)
            if result or include_empty:
                if meta_data:
                    param_data = self._data[param]
                    result = dict(param_data, **{"value": result})
                # Add "value" key to match marshmallow schema format.
                elif serializable:
                    result = {"value": result}
                all_params[param] = result
        if serializable:
            ser = self._defaults_schema.dump(all_params)
            # Unpack the values after serialization if meta_data not specified.
            if not meta_data:
                ser = {param: value["value"] for param, value in ser.items()}
            return ser
        else:
            return all_params

    def to_array(self, param):
        """
        Convert a Value object to an n-labelal array. The list of Value
        objects must span the specified parameter space. The parameter space
        is defined by inspecting the label validators in schema.json
        and the state attribute of the Parameters instance.

        Returns: n-labelal NumPy array.

        Raises:
            InconsistentLabelsException: Value objects do not have consistent
                labels.
            SparseValueObjectsException: Value object does not span the
                entire space specified by the Order object.
            ParamToolsError: Parameter is an array type and has labels.
                This is not supported by ParamTools when using array_first.
        """
        value_items = self.select_eq(param, False, **self._state)
        if not value_items:
            return np.array([])
        label_order, value_order = self._resolve_order(param, value_items)
        shape = []
        for label in label_order:
            shape.append(len(value_order[label]))
        shape = tuple(shape)
        # Compare len value items with the expected length if they are full.
        # In the futute, sparse objects should be supported by filling in the
        # unspecified labels.
        number_dims = self._data[param].get("number_dims", 0)
        if not shape and number_dims > 0:
            return np.array(
                value_items[0]["value"], dtype=self._numpy_type(param)
            )
        elif shape and number_dims > 0:
            raise ParamToolsError(
                f"\nParameter '{param}' is an array parameter with {number_dims} dimension(s) and "
                f"has labels: {', '.join(label_order)}.\n\nParamTools does not "
                f"support the use of 'array_first' with array parameters that use labels. "
                f"\nYou may be able to describe this parameter's values with additional "
                f"labels\nand the 'label_to_extend' operator."
            )
        elif not shape and number_dims == 0:
            data_type = self._numpy_type(param)
            value = value_items[0]["value"]
            if data_type == object:
                return value
            else:
                return data_type(value)
        exp_full_shape = reduce(lambda x, y: x * y, shape)
        if len(value_items) != exp_full_shape:
            # maintains label value order over value objects.
            exp_grid = list(itertools.product(*value_order.values()))
            # preserve label value order for each value object by
            # iterating over label_order.
            actual = set(
                [tuple(vo[d] for d in label_order) for vo in value_items]
            )
            missing = "\n\t".join(
                [str(d) for d in exp_grid if d not in actual]
            )
            raise SparseValueObjectsException(
                f"The Value objects for {param} do not span the specified "
                f"parameter space. Missing combinations:\n\t{missing}"
            )

        def list_2_tuple(x):
            return tuple(x) if isinstance(x, list) else x

        arr = np.empty(shape, dtype=self._numpy_type(param))

        for vi in value_items:
            # ix stores the indices of `arr` that need to be filled in.
            ix = [[] for i in range(len(label_order))]
            for label_pos, label_name in enumerate(label_order):
                # assume value_items is dense in the sense that it spans
                # the label space.
                ix[label_pos].append(
                    value_order[label_name].index(vi[label_name])
                )
            ix = tuple(map(list_2_tuple, ix))
            arr[ix] = vi["value"]
        return arr

    def from_array(self, param, array=None):
        """
        Convert NumPy array to a Value object.

        Returns:
            Value object (shape: [{"value": val, labels:...}])

        Raises:
            InconsistentLabelsException: Value objects do not have consistent
                labels.
        """
        if array is None:
            array = getattr(self, param)
            if not isinstance(array, np.ndarray):
                raise TypeError(
                    "A NumPy Ndarray should be passed to this method "
                    "or the instance attribute should be an array."
                )
        value_items = self.select_eq(param, False, **self._state)
        label_order, value_order = self._resolve_order(param, value_items)
        label_values = itertools.product(*value_order.values())
        label_indices = itertools.product(
            *map(lambda x: range(len(x)), value_order.values())
        )
        value_items = []
        for dv, di in zip(label_values, label_indices):
            vi = {label_order[j]: dv[j] for j in range(len(dv))}
            vi["value"] = array[di]
            value_items.append(vi)
        return value_items

    def extend(
        self,
        label_to_extend=None,
        label_to_extend_values=None,
        params=None,
        raise_errors=True,
        ignore_warnings=False,
    ):
        """
        Extend parameters along label_to_extend.

        Raises:
            InconsistentLabelsException: Value objects do not have consistent
                labels.
        """
        if label_to_extend is None:
            label_to_extend = self.label_to_extend

        spec = self.specification(meta_data=True)
        if params is not None:
            spec = {
                param: self._data[param]
                for param, data in spec.items()
                if param in params
            }
        extend_grid = (
            label_to_extend_values
            or self._stateless_label_grid[label_to_extend]
        )
        adjustment = defaultdict(list)
        for param, data in spec.items():
            if not any(label_to_extend in vo for vo in data["value"]):
                continue
            extended_vos = set()
            for vo in sorted(
                data["value"],
                key=lambda val: extend_grid.index(val[label_to_extend]),
            ):
                hashable_vo = utils.hashable_value_object(vo)
                if hashable_vo in extended_vos:
                    continue
                else:
                    extended_vos.add(hashable_vo)
                gt = select_gt_ix(
                    self._data[param]["value"],
                    False,
                    {label_to_extend: vo[label_to_extend]},
                    extend_grid,
                    tree=self._search_trees.get(param),
                )
                eq = select_eq(
                    gt,
                    False,
                    utils.filter_labels(vo, drop=["value", label_to_extend]),
                )
                extended_vos.update(map(utils.hashable_value_object, eq))
                eq += [vo]

                defined_vals = {eq_vo[label_to_extend] for eq_vo in eq}

                missing_vals = sorted(
                    set(extend_grid) - defined_vals,
                    key=lambda val: extend_grid.index(val),
                )

                if not missing_vals:
                    continue

                extended = defaultdict(list)

                for val in missing_vals:
                    eg_ix = extend_grid.index(val)
                    if eg_ix == 0:
                        first_defined_value = min(
                            defined_vals,
                            key=lambda val: extend_grid.index(val),
                        )
                        value_objects = select_eq(
                            eq, True, {label_to_extend: first_defined_value}
                        )
                    elif extend_grid[eg_ix - 1] in extended:
                        value_objects = extended.pop(extend_grid[eg_ix - 1])
                    else:
                        prev_defined_value = extend_grid[eg_ix - 1]
                        value_objects = select_eq(
                            eq, True, {label_to_extend: prev_defined_value}
                        )
                    # In practice, value_objects has length one.
                    # Theoretically, there could be multiple if the inital value
                    # object had less labels than later value objects and thus
                    # matched multiple value objects.
                    for value_object in value_objects:
                        ext = dict(value_object, **{label_to_extend: val})
                        ext = self.extend_func(
                            param,
                            ext,
                            value_object,
                            extend_grid,
                            label_to_extend,
                        )
                        extended_vos.add(
                            utils.hashable_value_object(value_object)
                        )
                        extended[val].append(ext)
                        adjustment[param].append(ext)
        # Ensure that the adjust method of paramtools.Parameter is used
        # in case the child class also implements adjust.
        self._adjust(
            adjustment,
            extend_adj=False,
            ignore_warnings=ignore_warnings,
            raise_errors=raise_errors,
        )

    def extend_func(
        self,
        param: str,
        extend_vo: ValueObject,
        known_vo: ValueObject,
        extend_grid: List,
        label_to_extend: str,
    ):
        """
        Function for applying indexing rates to parameter values as they
        are extended. Projects may implement their own extend_func by
        overriding this one. Projects need to write their own indexing_rate
        method for returning the correct indexing rate for a given parameter
        and value of label_to_extend (abbreviated to lte_val).

        returns: extended_vo
        """
        if not self.uses_extend_func or not self._data[param].get(
            "indexed", False
        ):
            return extend_vo

        known_val = known_vo[label_to_extend]
        known_ix = extend_grid.index(known_val)

        toext_val = extend_vo[label_to_extend]
        toext_ix = extend_grid.index(toext_val)

        if toext_ix > known_ix:
            # grow value according to the index rate supplied by the user defined
            # self.indexing_rate method.
            v = extend_vo["value"] * (
                1 + self.get_index_rate(param, known_val)
            )
            extend_vo["value"] = np.round(v, 2) if v < 9e99 else 9e99
        else:
            # shrink value according to the index rate supplied by the user defined
            # self.indexing_rate method.
            for ix in reversed(range(toext_ix, known_ix)):
                v = (
                    extend_vo["value"]
                    * (1 + self.get_index_rate(param, extend_grid[ix])) ** -1
                )
                extend_vo["value"] = np.round(v, 2) if v < 9e99 else 9e99
        return extend_vo

    def get_index_rate(self, param: str, lte_val: Any):
        """
        Return the value of the index_rates dictionary matching the
        label to extend value, `lte_val`.
        Projects may find it convenient to override this method with their own
        `index_rate` method.
        """
        return self.index_rates[lte_val]

    def _set_state(self, params=None, **labels):
        """
        Private method for setting the state on a Parameters instance. Internal
        methods can set which params will be updated. This is helpful when a set
        of parameters are adjusted and only their attributes need to be updated.
        """
        messages = {}
        for name, values in labels.items():
            if name not in self.label_validators:
                messages[name] = f"{name} is not a valid label."
                continue
            if not isinstance(values, list):
                values = [values]
            for value in values:
                try:
                    self.label_validators[name].deserialize(value)
                except MarshmallowValidationError as ve:
                    messages[name] = str(ve)
        if messages:
            raise ValidationError({"errors": messages}, labels=None)
        self._state.update(labels)
        for label_name, label_value in self._state.items():
            if not isinstance(label_value, list):
                label_value = [label_value]
            self.label_grid[label_name] = label_value
        spec = self.specification(include_empty=True, **self._state)
        if params is not None:
            spec = {param: spec[param] for param in params}
        for name, value in spec.items():
            if name in collision_list:
                raise ParameterNameCollisionException(
                    f"The paramter name, '{name}', is already used by the Parameters object."
                )
            if self.array_first:
                setattr(self, name, self.to_array(name))
            else:
                setattr(self, name, value)

    def _resolve_order(self, param, value_items):
        """
        Resolve the order of the labels and their values by
        inspecting data in the label grid values.

        The label grid for all labels is stored in the label_grid
        attribute. The labels to be used are the ones that are specified
        for each value object. Note that the labels must be specified
        _consistently_ for all value objects, i.e. none can be added or omitted
        for any value object in the list.

        Returns:
            label_order: The label order.
            value_order: The values, in order, for each label.

        Raises:
            InconsistentLabelsException: Value objects do not have consistent
                labels.
        """
        used = utils.consistent_labels(value_items)
        if used is None:
            raise InconsistentLabelsException(
                f"were added or omitted for some value object(s)."
            )
        label_order, value_order = [], {}
        for label_name, label_values in self.label_grid.items():
            if label_name in used:
                label_order.append(label_name)
                value_order[label_name] = label_values
        return label_order, value_order

    def _numpy_type(self, param):
        """
        Get the numpy type for a given parameter.
        """
        return (
            self._validator_schema.fields[param].schema.fields["value"].np_type
        )

    def select_eq(self, param, exact_match, **labels):
        return select_eq(
            self._data[param]["value"],
            exact_match,
            labels,
            tree=self._search_trees.get(param),
        )

    def select_gt(self, param, exact_match, **labels):
        return select_gt(
            self._data[param]["value"],
            exact_match,
            labels,
            tree=self._search_trees.get(param),
        )

    def _update_param(self, param, new_values):
        """
        Update the current parameter values with those specified by
        the adjustment. The values that need to be updated are chosen
        by finding all value items with label values matching the
        label values specified in the adjustment. If the value is
        set to None, then that value object will be removed.

        Note: _update_param used to raise a ParameterUpdateException if one of the new
            values did not match at least one of the current value objects. However,
            this was dropped to better support the case where the parameters are being
            extended along some label to fill the parameter space. An exception could
            be raised if a new value object contains a label that is not used in the
            current value objects for the parameter. However, it seems like it could be
            expensive to check this case, especially when a project is extending parameters.
            For now, no exceptions are raised by this method.

        """
        curr_values = self._data[param]["value"]
        if param in self._search_trees:
            curr_tree = self._search_trees[param]
        else:
            curr_tree = Tree(vos=curr_values, label_grid=self.label_grid)
        new_tree = Tree(vos=new_values, label_grid=self.label_grid)
        self._data[param]["value"] = curr_tree.update(new_tree)
        self._search_trees[param] = curr_tree

    def _parse_validation_messages(self, messages, params):
        """Parse validation messages from marshmallow"""
        if messages.get("warnings"):
            self._warnings.update(
                self._parse_errors(messages.pop("warnings"), params)
            )
        self._errors.update(self._parse_errors(messages, params))

    def _parse_errors(self, messages, params):
        """
        Parse the error messages given by marshmallow.

        Marshamllow error structure:

        {
            "list_param": {
                0: {
                    "value": {
                        0: [err message for first item in value list]
                        i: [err message for i-th item in value list]
                    }
                },
                i-th value object: {
                    "value": {
                        0: [...],
                        ...
                    }
                },
            }
            "nonlist_param": {
                0: {
                    "value": [err message]
                },
                ...
            }
        }

        self._errors structure:
        {
            "messages": {
                "param": [
                    ["value": {0: [msg0, msg1, ...], other_bad_ix: ...},
                     "label0": {0: msg, ...} // if errors on label values.
                ],
                ...
            },
            "label": {
                "param": [
                    {label_name: label_value, other_label_name: other_label_value},
                    ...
                    // list indices correspond to the error messages' indices
                    // of the error messages caused by the value of this value
                    // object.
                ]
            }
        }

        """
        error_info = {
            "messages": defaultdict(dict),
            "labels": defaultdict(dict),
        }

        for pname, data in messages.items():
            if pname == "_schema":
                error_info["messages"]["schema"] = [
                    f"Data format error: {data}"
                ]
                continue
            if data == ["Unknown field."]:
                error_info["messages"]["schema"] = [f"Unknown field: {pname}"]
                continue
            param_data = utils.ensure_value_object(params[pname])
            error_labels = []
            formatted_errors = []
            for ix, marshmessages in data.items():
                error_labels.append(
                    utils.filter_labels(param_data[ix], drop=["value"])
                )
                formatted_errors_ix = []
                for _, messages in marshmessages.items():
                    if messages:
                        if isinstance(messages, list):
                            formatted_errors_ix += messages
                        else:
                            for _, messagelist in messages.items():
                                formatted_errors_ix += messagelist
                formatted_errors.append(formatted_errors_ix)
            error_info["messages"][pname] = formatted_errors
            error_info["labels"][pname] = error_labels

        return error_info

    def __iter__(self):
        return iter(self._data)

    def keys(self):
        """
        Return parameter names.
        """
        return self._data.keys()

    def items(self):
        """
        Iterate using python dictionary .items() syntax.
        """
        for param in self:
            yield param, getattr(self, param)
        return

    def to_dict(self):
        """
        Return instance as python dictionary.
        """
        return dict(self.items())

    def sort_values(self, data=None, has_meta_data=True):
        """
        Sort value objects for all parameters in data according
        to the order specified in schema. If data is not specified,
        the existing value objects are used. User specified data
        can be:
            {param: [...value objects]}

            or

            {param: {"value": [...value objects]}}
        """

        def keyfunc(vo, label, label_values):
            if label in vo:
                return label_values.index(vo[label])
            else:
                return -1

        # nothing to do if labels aren't specified
        if not self._stateless_label_grid:
            return

        # iterate over labels so that the first label's order
        # takes precedence.
        label_grid = self._stateless_label_grid
        order = list(reversed(label_grid))

        if data is None:
            data = self._data
            update_attrs = True
            if not has_meta_data:
                raise ParamToolsError(
                    "has_meta_data must be True if data is not specified."
                )
        else:
            update_attrs = False

        for param, param_data in data.items():
            for label in order:
                label_values = label_grid[label]
                pfunc = partial(
                    keyfunc, label=label, label_values=label_values
                )
                if has_meta_data:
                    param_data["value"].sort(key=pfunc)
                else:
                    param_data.sort(key=pfunc)

            # Only update attributes when array first is off, since
            # value order will not affect how arrays are constructed.
            if update_attrs and has_meta_data and not self.array_first:
                setattr(self, param, param_data["value"])
