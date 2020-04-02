import inspect
import keyword
import logging
import re

from .server import pvfunction, PVGroup
from .._data import (ChannelDouble, ChannelEnum, ChannelFloat, ChannelChar,
                     ChannelInteger, ChannelString, ChannelByte)
from .menus import menus
from . import records as records_mod


logger = logging.getLogger(__name__)


def underscore_to_camel_case(s):
    'Convert abc_def_ghi -> AbcDefGhi'
    def capitalize_first(substring):
        return substring[:1].upper() + substring[1:]
    return ''.join(map(capitalize_first, s.split('_')))


def ophyd_component_to_caproto(attr, component, *, depth=0, dev=None):
    import ophyd

    indent = '    ' * depth
    sig = getattr(dev, attr) if dev is not None else None

    if isinstance(component, ophyd.DynamicDeviceComponent):
        to_describe = sig if sig is not None else component

        cpt_dict = ophyd_device_to_caproto_ioc(to_describe, depth=depth)
        cpt_name, = cpt_dict.keys()
        cpt_dict[''] = [
            '',
            f"{indent}{attr} = SubGroup({cpt_name}, prefix='')",
            '',
        ]
        return cpt_dict

    elif issubclass(component.cls, ophyd.Device):
        kwargs = dict()
        if isinstance(component, ophyd.FormattedComponent):
            # TODO Component vs FormattedComponent
            kwargs['name'] = "''"

        to_describe = sig if sig is not None else component.cls

        cpt_dict = ophyd_device_to_caproto_ioc(to_describe, depth=depth)
        cpt_name, = cpt_dict.keys()
        cpt_dict[''] = [
            '',
            (f"{indent}{attr} = SubGroup({cpt_name}, "
             f"prefix='{component.suffix}')"),
            '',
        ]
        return cpt_dict

    kwargs = dict(name=repr(component.suffix))

    if isinstance(component, ophyd.FormattedComponent):
        # TODO Component vs FormattedComponent
        kwargs['name'] = "''"
    else:  # if hasattr(component, 'suffix'):
        kwargs['name'] = repr(component.suffix)

    if sig and sig.connected:
        value = sig.get()

        def array_checker(value):
            try:
                import numpy as np
                return isinstance(value, np.ndarray)
            except ImportError:
                return False

        try:
            # NELM reflects the actual maximum length of the value, as opposed
            # to the current length
            max_length = sig._read_pv.nelm
        except Exception:
            max_length = 1

        if array_checker(value):
            # hack, as value can be a zero-length array
            # FUTURE_TODO: support numpy types directly in pvproperty type map
            import numpy as np
            value = np.zeros(1, dtype=value.dtype).tolist()[0]
        else:
            try:
                value = value[0]
            except (IndexError, TypeError):
                ...

        kwargs['dtype'] = type(value).__name__
        if max_length > 1:
            kwargs['max_length'] = max_length
    else:
        cpt_kwargs = getattr(component, 'kwargs', {})
        is_string = cpt_kwargs.get('string', False)
        if is_string:
            kwargs['dtype'] = 'str'
        else:
            kwargs['dtype'] = 'unknown'

    # if component.__doc__:
    #     kwargs['doc'] = repr(component.__doc__)

    if issubclass(component.cls, ophyd.EpicsSignalRO):
        kwargs['read_only'] = True

    kw_str = ', '.join(f'{key}={value}'
                       for key, value in kwargs.items())

    if issubclass(component.cls, ophyd.EpicsSignalWithRBV):
        line = f"{attr} = pvproperty_with_rbv({kw_str})"
    elif issubclass(component.cls, (ophyd.EpicsSignalRO, ophyd.EpicsSignal)):
        line = f"{attr} = pvproperty({kw_str})"
    else:
        line = f"# {attr} = pvproperty({kw_str})"

    # single line, no new subclass defined
    return {'': [' ' * (4 * depth) + line]}


def ophyd_device_to_caproto_ioc(dev, *, depth=0):
    import ophyd

    if isinstance(dev, ophyd.DynamicDeviceComponent):
        # DynamicDeviceComponent: attr: (sig_cls, prefix, kwargs)
        # NOTE: cannot inspect types without an instance of the dynamic Device
        # class
        attr_components = {
            attr: ophyd.Component(sig_cls, prefix, **kwargs)
            for attr, (sig_cls, prefix, kwargs) in dev.defn.items()
        }
        dev_name = f'{dev.attr}_group'
        cls, dev = dev, None
    else:
        if inspect.isclass(dev):
            # we can introspect Device directly, but we cannot connect to PVs
            # and tell about their data type
            cls, dev = dev, None
        else:
            # if connected, we can reach out to PVs and determine data types
            cls = dev.__class__
        attr_components = cls._sig_attrs
        dev_name = f'{cls.__name__}_group'

    dev_name = underscore_to_camel_case(dev_name)
    indent = '    ' * depth

    dev_lines = ['',
                 f"{indent}class {dev_name}(PVGroup):"]

    for attr, component in attr_components.items():
        cpt_lines = ophyd_component_to_caproto(attr, component,
                                               depth=depth + 1,
                                               dev=dev)
        if isinstance(cpt_lines, dict):
            # new device/sub-group, for now add it on
            for lines in cpt_lines.values():
                dev_lines.extend(lines)
        else:
            dev_lines.extend(cpt_lines)

    return {dev_name: dev_lines}


def pvfunction_to_device_function(name, pvf, *, indent='    '):
    'pvfunction -> Device method'
    def format_arg(pvspec):
        value = pvspec.value
        if isinstance(value, (list, tuple)) and len(value) == 1:
            value = value[0]
        value = f'={value}' if value else ''
        return f"{pvspec.attr}: {pvspec.dtype.__name__}{value}"

    skip_attrs = ('status', 'retval')
    args = ', '.join(format_arg(spec) for spec in pvf.pvspec
                     if spec.attr not in skip_attrs)
    yield f"{indent}def call(self, {args}):"
    if pvf.__doc__:
        yield f"{indent*2}'{pvf.__doc__}'"
    for pvspec in pvf.pvspec:
        if pvspec.attr not in skip_attrs:
            yield (f"{indent*2}self.{pvspec.attr}.put({pvspec.attr}, "
                   "wait=True)")

    yield f"{indent*2}self.process.put(1, wait=True)"
    yield f"{indent*2}status = self.status.get(use_monitor=False)"
    yield f"{indent*2}retval = self.retval.get(use_monitor=False)"
    yield f"{indent*2}if status != 'Success':"
    yield f"{indent*3}raise RuntimeError(f'RPC function failed: {{status}}')"
    yield f"{indent*2}return retval"


def group_to_device(group):
    'Make an ophyd device from a high-level server PVGroup'
    # TODO subgroups are weak and need rethinking (generic comment deux)

    for name, subgroup in group._subgroups_.items():
        yield from group_to_device(subgroup.group_cls)

        if isinstance(subgroup, pvfunction):
            yield f''
            yield from pvfunction_to_device_function(name, subgroup)

        yield f''
        yield f''

    if isinstance(group, PVGroup):
        group = group.__class__

    yield f"class {group.__name__}Device(ophyd.Device):"

    for name, subgroup in group._subgroups_.items():
        doc = f', doc={subgroup.__doc__!r}' if subgroup.__doc__ else ''
        yield (f"    {name.lower()} = Cpt({name}Device, '{subgroup.prefix}'"
               f"{doc})")

    if not group._pvs_:
        yield f'    ...'

    for name, prop in group._pvs_.items():
        if '.' in name:
            # Skipping, part of subgroup handled above
            continue

        pvspec = prop.pvspec
        doc = f', doc={pvspec.doc!r}' if pvspec.doc else ''
        string = f', string=True' if pvspec.dtype == str else ''
        cls = 'EpicsSignalRO' if pvspec.read_only else 'EpicsSignal'
        yield (f"    {name.lower()} = Cpt({cls}, '{pvspec.name}'" f"{string}{doc})")
        # TODO will break when full/macro-ified PVs is specified

    # lower_name = group.__name__.lower()
    # yield f"# {lower_name} = {group.__name__}Device(my_prefix)"


def get_base_fields(dbd_info, *, model_record='ai'):
    'Get fields that are common to all record types'
    common_fields = None
    for record_type, fields in dbd_info.items():
        fset = set((field, finfo['type'], finfo.get('size', 0))
                   for field, finfo in fields.items())
        if common_fields is None:
            common_fields = fset
        else:
            common_fields = fset.intersection(common_fields)

    return {field: dbd_info[model_record][field]
            for field, ftype, fsize in common_fields}


DBD_TYPE_INFO = {
    'DBF_DEVICE': ChannelEnum,
    'DBF_FLOAT': ChannelFloat,
    'DBF_DOUBLE': ChannelDouble,
    'DBF_FWDLINK': ChannelString,
    'DBF_INLINK': ChannelString,
    'DBF_INT64': ChannelInteger,
    'DBF_LONG': ChannelInteger,
    'DBF_MENU': ChannelEnum,
    'DBF_ENUM': ChannelEnum,
    'DBF_OUTLINK': ChannelString,
    'DBF_SHORT': ChannelInteger,
    'DBF_STRING': ChannelString,
    'DBF_CHAR': ChannelChar,

    # unsigned types which don't actually have ChannelType equivalents:
    'DBF_UCHAR': ChannelByte,
    'DBF_ULONG': ChannelInteger,
    'DBF_USHORT': ChannelInteger,
}


DTYPE_OVERRIDES = {
    # DBF_SHORT is ChannelInteger -> LONG; override with SHORT (=INT)
    'DBF_SHORT': 'INT',
    'DBF_USHORT': 'INT',
}

FIELD_OVERRIDE_OK = {'DTYP', }


def record_to_field_info(record_type, dbd_info, dtypes):
    'Yield field information for a given record, removing base fields'
    base_metadata = get_base_fields(dbd_info)
    field_dict = (base_metadata if record_type == 'base'
                  else dbd_info[record_type])

    for field_name, field_info in field_dict.items():
        type_ = field_info['type']
        if type_ in {'DBF_NOACCESS', }:
            continue
        if record_type != 'base' and field_name in base_metadata:
            if field_name in FIELD_OVERRIDE_OK:
                # Allow this one through
                ...
            elif base_metadata[field_name] == field_info:
                # Skip base attrs
                continue

        size = field_info.get('size', 0)
        prompt = field_info['prompt']

        # alarm = parent.alarm
        kwargs = {}

        if type_ == 'DBF_STRING' and size > 0:
            type_ = 'DBF_UCHAR'
            kwargs['max_length'] = size
            kwargs['report_as_string'] = True
        elif size > 1:
            kwargs['max_length'] = size

        if type_ == 'DBF_MENU':
            # note: ordered key assumption here (py3.6+)
            kwargs['enum_strings'] = (f'menus.{field_info["menu"]}'
                                      '.get_string_tuple()')
        elif type_ == 'DBF_DEVICE':
            if not dtypes:
                # TODO: empty enums are problematic for caproto at the moment
                dtypes = ('Soft Channel', )
            kwargs['enum_strings'] = repr(tuple(dtypes))

        if prompt:
            kwargs['doc'] = repr(prompt)

        if field_info.get('special') == 'SPC_NOMOD':
            kwargs['read_only'] = True

        type_class = DBD_TYPE_INFO[type_]
        attr_name = get_attr_name_from_dbd_prompt(
            record_type, field_name, prompt)
        yield attr_name, type_class, kwargs, field_info


def get_attr_name_from_dbd_prompt(record_type, field_name, prompt):
    'Attribute name for fields: e.g., "Sim. Mode Scan" -> "sim_mode_scan"'
    attr_name = prompt.lower()
    # If there's a parenthesized part not at the beginning, remove it:
    # e.g., "Velocity (EGU/s)" -> "Velocity"
    if '(' in attr_name and not attr_name.startswith('('):
        attr_name = attr_name.split('(')[0].strip()

    # Replace bad characters with _
    attr_name = re.sub('[^a-z_0-9]', '_', attr_name, flags=re.IGNORECASE)
    # Replace multiple ___ -> single _
    attr_name = re.sub('_+', '_', attr_name)
    # Remove starting/ending _
    attr_name = attr_name.strip('_') or field_name.lower()
    if keyword.iskeyword(attr_name):
        attr_name = f'{attr_name}_'
    return _fix_attr_name(record_type, attr_name, field_name)


def _fix_attr_name(record_type, attr_name, field_name):
    'Apply any transformations specified in ATTR_RENAMES/REPLACES'
    manual_fix = ATTR_FIXES.get(record_type, {}).get(field_name, None)
    if manual_fix is not None:
        return manual_fix

    attr_name = ATTR_RENAMES.get(attr_name, attr_name)
    for re_replace, repl_to in ATTR_REPLACES.items():
        attr_name = re_replace.sub(repl_to, attr_name)
    return attr_name


LINKABLE = {
    'display_precision': 'precision',
    'hihi_alarm_limit': 'upper_alarm_limit',
    'high_alarm_limit': 'upper_warning_limit',
    'low_alarm_limit': 'lower_warning_limit',
    'lolo_alarm_limit': 'lower_alarm_limit',
    'high_operating_range': 'upper_ctrl_limit',
    'low_operating_range': 'lower_ctrl_limit',
    # 'alarm_deadband': '',
    'archive_deadband': 'log_atol',
    'monitor_deadband': 'value_atol',
    'maximum_elements': 'length',
    'number_of_elements': 'max_length',
}

LINK_KWARGS = {
    'archive_deadband': dict(use_setattr=True),
    'monitor_deadband': dict(use_setattr=True),
    'maximum_elements': dict(use_setattr=True, read_only=True),
    'number_of_elements': dict(use_setattr=True, read_only=True),
}


ATTR_RENAMES = {
    # back-compat
    'scan_mechanism': 'scan_rate',
    'descriptor': 'description',
    'force_processing': 'process_record',
    'forward_process_link': 'forward_link',
}
ATTR_FIXES = {
    'aSub': {
        'ONVH': 'num_elements_in_ovlh',
    },
}

ATTR_REPLACES = {
    re.compile('^alarm_ack_'): 'alarm_acknowledge_',
    re.compile('_sevrty$'): '_severity',
}

MIXINS = (records_mod._Limits, records_mod._LimitsLong)
MIXIN_SPECS = {
    mixin: {pv.pvspec.name: pv.pvspec.dtype for pv in mixin._pvs_.values()}
    for mixin in MIXINS
}


def record_to_template_dict(record_type, dbd_info, dtypes, *,
                            skip_fields=None):
    'Record name -> yields code to create a PVGroup for all fields'
    if skip_fields is None:
        skip_fields = ['VAL']

    result = {
        'record_type': record_type,
        'class_name': f'{record_type.capitalize()}Fields',
        'dtype': None,
        'base_class': 'RecordFieldGroup',
        'mixin': '',
        'fields': [],
        'links': [],
    }

    if record_type == 'base':
        result['class_name'] = 'RecordFieldGroup'
        result['base_class'] = 'PVGroup'
        result['record_type'] = None
        existing_record = records_mod.RecordFieldGroup
    else:
        field_info = dbd_info[record_type]
        val_type = field_info.get('VAL', {'type': 'DBF_NOACCESS'})['type']
        if val_type != 'DBF_NOACCESS':
            val_channeltype = DBD_TYPE_INFO[val_type].data_type.name
            result['dtype'] = "ChannelType." + val_channeltype
        existing_record = records_mod.records.get(record_type)

    existing_field_list = []
    if existing_record:
        existing_field_list = [pvprop.pvspec.name
                               for pvprop in existing_record._pvs_.values()]

    fields_by_attr = {}
    field_to_attr = {}

    for item in record_to_field_info(record_type, dbd_info,
                                     dtypes.get(record_type, [])):
        attr_name, cls, kwargs, finfo = item
        # note to self: next line is e.g.,
        #   ChannelDouble -> ChannelDouble(, ... dtype=ChannelType.FLOAT)
        dtype = DTYPE_OVERRIDES.get(finfo['type'], cls.data_type.name)
        comment = False
        if finfo['field'] in skip_fields:
            comment = True
        elif finfo.get('menu') and finfo['menu'] not in menus:
            comment = True

        field_name = finfo['field']  # as in EPICS
        fields_by_attr[attr_name] = dict(
            attr=attr_name,
            field_name=field_name,
            dtype=dtype,
            kwargs=kwargs,
            comment=comment,
            sort_id=_get_sort_id(field_name, existing_field_list),
        )
        field_to_attr[field_name] = attr_name

    if record_type != 'base':
        for mixin, mixin_info in MIXIN_SPECS.items():
            has_fields = all(field in field_info
                             for field in mixin_info)
            types_match = all(
                DBD_TYPE_INFO[field_info[field]['type']].data_type == mixin_info[field]
                for field in mixin_info
                if field in field_info
            )
            if has_fields and types_match:
                # Add the mixin
                result['mixin'] = [mixin.__name__]
                # And remove those attributes from the subclass
                for field in mixin_info:
                    fields_by_attr.pop(field_to_attr[field])

    for field_attr, field in fields_by_attr.items():
        result['fields'].append(field)
        try:
            channeldata_attr = LINKABLE[field_attr]
        except KeyError:
            ...
        else:
            link = dict(field_attr=field_attr,
                        channeldata_attr=channeldata_attr,
                        kwargs=LINK_KWARGS.get(field_attr, {}),
                        )
            result['links'].append(link)

    result['field_to_attr'] = field_to_attr
    return result


def _get_sort_id(name, list_):
    'Returns tuple of (index in list, name)'
    key1 = (list_.index(name)
            if name in list_
            else len(list_) + 1
            )
    return (key1, name)


def generate_all_records_jinja(dbd_file, *, jinja_env=None,
                               template='records.jinja2'):
    try:
        from caproto.tests.dbd import DbdFile
        import jinja2
    except ImportError as ex:
        raise ImportError(f'An optional/testing dependency is missing: {ex}')

    if jinja_env is None:
        jinja_env = jinja2.Environment(
            loader=jinja2.PackageLoader("caproto", "server"),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    dbd_file = DbdFile.parse_file(dbd_file)
    existing_record_list = ['base'] + list(records_mod.records)
    records = {}
    for record in ('base', ) + tuple(sorted(dbd_file.field_metadata)):
        d = record_to_template_dict(record, dbd_file.field_metadata,
                                    dbd_file.record_dtypes)
        d['sort_id'] = _get_sort_id(record, existing_record_list)
        records[record] = d

    record_template = jinja_env.get_template(template)
    return record_template.render(records=records)
