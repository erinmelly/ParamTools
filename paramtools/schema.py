from collections import defaultdict

from marshmallow import (
    Schema,
    fields,
    validate,
    validates_schema,
    ValidationError as MarshmallowValidationError,
)

from paramtools import contrib
from paramtools import utils


class RangeSchema(Schema):
    """
    Schema for range object
    {
        "range": {"min": field, "max": field}
    }
    """

    _min = fields.Field(attribute="min", data_key="min")
    _max = fields.Field(attribute="max", data_key="max")
    level = fields.String(validate=[validate.OneOf(["warn", "error"])])


class ChoiceSchema(Schema):
    choices = fields.List(fields.Field)
    level = fields.String(validate=[validate.OneOf(["warn", "error"])])


class ValueValidatorSchema(Schema):
    """
    Schema for validation specification for each parameter value
    """

    _range = fields.Nested(
        RangeSchema(), attribute="range", data_key="range", required=False
    )
    date_range = fields.Nested(RangeSchema(), required=False)
    choice = fields.Nested(ChoiceSchema(), required=False)
    when = fields.Nested("WhenSchema", required=False)


class IsSchema(Schema):
    equal_to = fields.Field(required=False)
    greater_than = fields.Field(required=False)
    less_than = fields.Field(required=False)

    @validates_schema
    def just_one(self, data, **kwargs):
        if len(data.keys()) > 1:
            raise MarshmallowValidationError(
                f"Only one condition may be specified for the 'is' field. "
                f"You specified {len(data.keys())}."
            )

    def _deserialize(self, data, **kwargs):
        if data is not None and not isinstance(data, dict):
            data = {"equal_to": data}
        return super()._deserialize(data, **kwargs)


class WhenSchema(Schema):
    param = fields.Str()
    _is = fields.Nested(
        IsSchema(), attribute="is", data_key="is", required=False
    )
    then = fields.Nested(ValueValidatorSchema())
    otherwise = fields.Nested(ValueValidatorSchema())


class BaseParamSchema(Schema):
    """
    Defines a base parameter schema. This specifies the required fields and
    their types.
    {
        "title": str,
        "description": str,
        "notes": str,
        "type": str (limited to 'int', 'float', 'bool', 'str'),
        "value": `BaseValidatorSchema`, "value" type depends on "type" key,
        "range": range schema ({"min": ..., "max": ..., "other ops": ...}),
    }

    This class is defined further by a JSON file indicating extra fields that
    are required by the implementer of the schema.
    """

    title = fields.Str(required=True)
    description = fields.Str(required=True)
    notes = fields.Str(required=False)
    _type = fields.Str(
        required=True,
        validate=validate.OneOf(
            choices=["str", "float", "int", "bool", "date"]
        ),
        attribute="type",
        data_key="type",
    )
    number_dims = fields.Integer(required=False, missing=0)
    value = fields.Field(required=True)  # will be specified later
    validators = fields.Nested(
        ValueValidatorSchema(), required=False, missing={}
    )
    indexed = fields.Boolean(required=False)


class EmptySchema(Schema):
    """
    An empty schema that is used as a base class for creating other classes via
    the `type` function
    """

    pass


class OrderedSchema(Schema):
    """
    Same as `EmptySchema`, but preserves the order of its fields.
    """

    class Meta:
        ordered = True


class ValueObject(fields.Nested):
    """
    Schema for value objects
    """

    def _deserialize(
        self, value, attr, data, partial=None, many=False, **kwargs
    ):
        if not isinstance(value, list) or (
            isinstance(value, list)
            and value
            and not isinstance(value[0], dict)
        ):
            value = [{"value": value}]
        return super()._deserialize(
            value, attr, data, partial=partial, many=many, **kwargs
        )


class BaseValidatorSchema(Schema):
    """
    Schema that validates parameter adjustments such as:
    ```
    {
        "STD": [{
            "year": 2017,
            "MARS": "single",
            "value": "3000"
        }]
    }
    ```

    Information defined for each variable on the `BaseParamSchema` is utilized
    to define this class and how it should validate its data. See
    `build_schema.SchemaBuilder` for how parameters are defined onto this
    class.
    """

    class Meta:
        ordered = True

    WRAPPER_MAP = {
        "range": "_get_range_validator",
        "date_range": "_get_range_validator",
        "choice": "_get_choice_validator",
        "when": "_get_when_validator",
    }

    def load(self, data, ignore_warnings):
        self.ignore_warnings = ignore_warnings
        try:
            return super().load(data)
        finally:
            self.ignore_warnings = False

    @validates_schema
    def validate_params(self, data, **kwargs):
        """
        Loop over all parameters defined on this class. Validate them using
        the `self.validate_param`. Errors are stored until all
        parameters have been validated. Note that all data has been
        type-validated. These methods only do range validation.
        """
        warnings = defaultdict(dict)
        errors = defaultdict(dict)
        for name, specs in data.items():
            for i, spec in enumerate(specs):
                _warnings, _errors = self.validate_param(name, spec, data)
                if _warnings:
                    warnings[name][i] = {"value": _warnings}
                if _errors:
                    errors[name][i] = {"value": _errors}
        if warnings and not self.ignore_warnings:
            errors["warnings"] = warnings
        if errors:
            ve = MarshmallowValidationError(dict(errors))
            raise ve

    def validate_param(self, param_name, param_spec, raw_data):
        """
        Do range validation for a parameter.
        """
        param_info = self.context["spec"]._data[param_name]
        # sort keys to guarantee order.
        validator_spec = param_info["validators"]
        validators = []
        for vname, vdata in validator_spec.items():
            validator = getattr(self, self.WRAPPER_MAP[vname])(
                vname, vdata, param_name, param_spec, raw_data
            )
            validators.append(validator)

        warnings = []
        errors = []
        for validator in validators:
            try:
                validator(param_spec, is_value_object=True)
            except contrib.validate.ValidationError as ve:
                if ve.level == "warn":
                    warnings += ve.messages
                else:
                    errors += ve.messages

        return warnings, errors

    def _get_when_validator(
        self,
        vname,
        when_dict,
        param_name,
        param_spec,
        raw_data,
        ndim_restriction=False,
    ):
        when_param = when_dict["param"]

        if (
            when_param not in self.context["spec"]._data.keys()
            and when_param != "default"
        ):
            raise MarshmallowValidationError(
                f"'{when_param}' is not a specified parameter."
            )

        oth_param, when_vos = self._resolve_op_value(
            when_param, param_name, param_spec, raw_data
        )
        then_validators = []
        for vname, vdata in when_dict["then"].items():
            then_validators.append(
                getattr(self, self.WRAPPER_MAP[vname])(
                    vname,
                    vdata,
                    param_name,
                    param_spec,
                    raw_data,
                    ndim_restriction=True,
                )
            )
        otherwise_validators = []
        for vname, vdata in when_dict["otherwise"].items():
            otherwise_validators.append(
                getattr(self, self.WRAPPER_MAP[vname])(
                    vname,
                    vdata,
                    param_name,
                    param_spec,
                    raw_data,
                    ndim_restriction=True,
                )
            )

        _type = self.context["spec"]._data[oth_param]["type"]
        number_dims = self.context["spec"]._data[oth_param]["number_dims"]

        error_then = (
            f"When {oth_param}{{when_labels}}{{ix}} is {{is_val}}, "
            f"{param_name}{{labels}}{{ix}} value is invalid: {{submsg}}"
        )
        error_otherwise = (
            f"When {oth_param}{{when_labels}}{{ix}} is not {{is_val}}, "
            f"{param_name}{{labels}}{{ix}} value is invalid: {{submsg}}"
        )

        return contrib.validate.When(
            when_dict["is"],
            when_vos,
            then_validators,
            otherwise_validators,
            error_then,
            error_otherwise,
            _type,
            number_dims,
        )

    def _get_range_validator(
        self,
        vname,
        range_dict,
        param_name,
        param_spec,
        raw_data,
        ndim_restriction=False,
    ):
        if vname == "range":
            range_class = contrib.validate.Range
        elif vname == "date_range":
            range_class = contrib.validate.DateRange
        else:
            raise MarshmallowValidationError(
                f"{vname} is not an allowed validator."
            )
        min_value = range_dict.get("min", None)
        if min_value is not None:
            min_oth_param, min_vos = self._resolve_op_value(
                min_value, param_name, param_spec, raw_data
            )
        else:
            min_oth_param, min_vos = None, []

        max_value = range_dict.get("max", None)
        if max_value is not None:
            max_oth_param, max_vos = self._resolve_op_value(
                max_value, param_name, param_spec, raw_data
            )
        else:
            max_oth_param, max_vos = None, []
        self._check_ndim_restriction(
            param_name,
            min_oth_param,
            max_oth_param,
            ndim_restriction=ndim_restriction,
        )
        min_vos = self._sort_by_label_to_extend(min_vos)
        max_vos = self._sort_by_label_to_extend(max_vos)
        error_min = f"{param_name}{{labels}} {{input}} < min {{min}} {min_oth_param}{{oth_labels}}"
        error_max = f"{param_name}{{labels}} {{input}} > max {{max}} {max_oth_param}{{oth_labels}}"
        return range_class(
            min_vo=min_vos,
            max_vo=max_vos,
            error_min=error_min,
            error_max=error_max,
            level=range_dict.get("level"),
        )

    def _sort_by_label_to_extend(self, vos):
        label_to_extend = self.context["spec"].label_to_extend
        if label_to_extend is not None:
            label_grid = self.context["spec"]._stateless_label_grid
            extend_vals = label_grid[label_to_extend]
            return sorted(
                vos,
                key=lambda vo: (
                    extend_vals.index(vo[label_to_extend])
                    if label_to_extend in vo
                    and vo[label_to_extend] in extend_vals
                    else 9e99
                ),
            )
        else:
            return vos

    def _get_choice_validator(
        self,
        vname,
        choice_dict,
        param_name,
        param_spec,
        raw_data,
        ndim_restriction=False,
    ):
        choices = choice_dict["choices"]
        labels = utils.make_label_str(param_spec)
        label_suffix = f" for labels {labels}" if labels else ""
        if len(choices) < 20:
            error_template = (
                '{param_name} "{input}" must be in list of choices '
                "{choices}{label_suffix}."
            )
        else:
            error_template = '{param_name} "{input}" must be in list of choices{label_suffix}.'
        error = error_template.format(
            param_name=param_name,
            labels=labels,
            input="{input}",
            choices="{choices}",
            label_suffix=label_suffix,
        )
        return contrib.validate.OneOf(
            choices, error=error, level=choice_dict.get("level")
        )

    def _resolve_op_value(self, op_value, param_name, param_spec, raw_data):
        """
        Operator values (`op_value`) are the values pointed to by the "min"
        and "max" keys. These can be values to compare against, another
        variable to compare against, or the default value of the adjusted
        variable.
        """
        if op_value in self.fields or op_value == "default":
            return self._get_comparable_value(
                op_value, param_name, param_spec, raw_data
            )
        return "", [{"value": op_value}]

    def _get_comparable_value(
        self, oth_param_name, param_name, param_spec, raw_data
    ):
        """
        Get the value that the adjusted variable will be compared against.
        Candidates are:
        - the parameter's own default value if "default" is specified
        - a reference variable's value
          - first, look in the raw adjustment data
          - second, look in the defaults data
        """
        if oth_param_name in raw_data:
            vals = raw_data[oth_param_name]
        else:
            # If comparing against the "default" value then get the current
            # value of the parameter being updated.
            if oth_param_name == "default":
                oth_param = self.context["spec"]._data[param_name]
            else:
                oth_param = self.context["spec"]._data[oth_param_name]
            vals = oth_param["value"]
        labs_to_check = {k for k in param_spec if k != "value"}
        if labs_to_check:
            res = [
                val
                for val in vals
                if all(val[k] == param_spec[k] for k in labs_to_check)
            ]
        else:
            res = vals
        return oth_param_name, res

    def _check_ndim_restriction(
        self, param_name, *other_params, ndim_restriction=False
    ):
        """
        Test restriction on validator's concerning references to other
        parameters with number of dimensions >= 1.
        """
        if ndim_restriction and any(other_params):
            for other_param in other_params:
                if other_param is None:
                    continue
                if other_param == "default":
                    ndims = self.context["spec"]._data[param_name][
                        "number_dims"
                    ]
                else:
                    ndims = self.context["spec"]._data[other_param][
                        "number_dims"
                    ]
                if ndims > 0:
                    raise contrib.validate.ValidationError(
                        f"{param_name} is validated against {other_param} in an invalid context."
                    )


class LabelSchema(Schema):
    _type = fields.Str(
        required=True,
        validate=validate.OneOf(
            choices=["str", "float", "int", "bool", "date"]
        ),
        attribute="type",
        data_key="type",
    )
    number_dims = fields.Integer(required=False, missing=0)
    validators = fields.Nested(
        ValueValidatorSchema(), required=False, missing={}
    )


class AdditionalMembersSchema(Schema):
    _type = fields.Str(
        required=True,
        validate=validate.OneOf(
            choices=["str", "float", "int", "bool", "date"]
        ),
        attribute="type",
        data_key="type",
    )
    number_dims = fields.Integer(required=False, missing=0)


class OperatorsSchema(Schema):
    array_first = fields.Bool(required=False)
    label_to_extend = fields.Str(required=False, allow_none=True)
    uses_extend_func = fields.Bool(required=False)


class ParamToolsSchema(Schema):
    labels = fields.Dict(
        keys=fields.Str(),
        values=fields.Nested(LabelSchema()),
        required=False,
        missing={},
    )
    additional_members = fields.Dict(
        keys=fields.Str(),
        values=fields.Nested(AdditionalMembersSchema()),
        required=False,
        missing={},
    )
    operators = fields.Nested(OperatorsSchema, required=False)


# A few fields that have not been instantiated yet
CLASS_FIELD_MAP = {
    "str": contrib.fields.Str,
    "int": contrib.fields.Integer,
    "float": contrib.fields.Float,
    "bool": contrib.fields.Boolean,
    "date": contrib.fields.Date,
}


INVALID_NUMBER = {"invalid": "Not a valid number: {input}."}
INVALID_BOOLEAN = {"invalid": "Not a valid boolean: {input}."}
INVALID_DATE = {"invalid": "Not a valid date: {input}."}

# A few fields that have been instantiated
FIELD_MAP = {
    "str": contrib.fields.Str(allow_none=True),
    "int": contrib.fields.Integer(
        allow_none=True, error_messages=INVALID_NUMBER
    ),
    "float": contrib.fields.Float(
        allow_none=True, error_messages=INVALID_NUMBER
    ),
    "bool": contrib.fields.Boolean(
        allow_none=True, error_messages=INVALID_BOOLEAN
    ),
    "date": contrib.fields.Date(allow_none=True, error_messages=INVALID_DATE),
}

VALIDATOR_MAP = {
    "range": contrib.validate.Range,
    "date_range": contrib.validate.DateRange,
    "choice": contrib.validate.OneOf,
}


def get_type(data):
    numeric_types = {
        "int": contrib.fields.Int64(
            allow_none=True, error_messages=INVALID_NUMBER
        ),
        "bool": contrib.fields.Bool_(
            allow_none=True, error_messages=INVALID_BOOLEAN
        ),
        "float": contrib.fields.Float64(
            allow_none=True, error_messages=INVALID_NUMBER
        ),
    }
    types = dict(FIELD_MAP, **numeric_types)
    fieldtype = types[data["type"]]
    dim = data.get("number_dims", 0)
    while dim > 0:
        np_type = getattr(fieldtype, "np_type", object)
        fieldtype = fields.List(fieldtype, allow_none=True)
        fieldtype.np_type = np_type
        dim -= 1
    return fieldtype


def get_param_schema(base_spec, field_map=None):
    """
    Read in data from the initializing schema. This will be used to fill in the
    optional properties on classes derived from the `BaseParamSchema` class.
    This data is also used to build validators for schema for each parameter
    that will be set on the `BaseValidatorSchema` class
    """
    if field_map is not None:
        field_map = dict(FIELD_MAP, **field_map)
    else:
        field_map = FIELD_MAP.copy()
    optional_fields = {}
    for k, v in base_spec["additional_members"].items():
        fieldtype = field_map[v["type"]]
        if v.get("number_dims", 0) > 0:
            d = v["number_dims"]
            while d > 0:
                fieldtype = fields.List(fieldtype)
                d -= 1
        optional_fields[k] = fieldtype

    ParamSchema = type(
        "ParamSchema",
        (BaseParamSchema,),
        {k: v for k, v in optional_fields.items()},
    )
    label_validators = {}
    for name, label in base_spec["labels"].items():
        validators = []
        for vname, kwargs in label["validators"].items():
            validator_class = VALIDATOR_MAP[vname]
            validators.append(validator_class(**kwargs))
        fieldtype = CLASS_FIELD_MAP[label["type"]]
        label_validators[name] = fieldtype(validate=validators)
    return ParamSchema, label_validators
