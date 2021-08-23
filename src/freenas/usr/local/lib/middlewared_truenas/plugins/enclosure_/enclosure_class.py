import logging

from .enclosure.element_types import ELEMENT_TYPES, ELEMENT_DESC, ELEMENT_ACTION
from .enclosure.regex import RE


logger = logging.getLogger(__name__)


class Enclosure(object):

    def __init__(self, stat, product):
        self.product = product
        self.stat = stat
        self.num = [i for i in self.stat][0]
        self.devname = f'ses{self.num}'
        self.encname = self.stat[self.num]['name']
        self.encid = self.stat[self.num]['id']
        self.model = self._set_model()
        self.controller = False
        self.status = ','.join(self.stat[self.num]['status'])
        self.elements = self._parse_elements()

    @property
    def num(self):
        return self.num

    @property
    def devname(self):
        return self.devname

    @property
    def encname(self):
        return self.encname

    @property
    def encid(self):
        return self.encid

    @property
    def model(self):
        return self.model

    @property
    def controller(self):
        return self.controller

    @property
    def status(self):
        return self.status

    @property
    def elements(self):
        return self.elements

    def _set_model(self):
        # enclosure head-unit detection
        if RE.M.value.match(self.encname):
            self.model = 'M Series'
            self.controller = True
        elif RE.X.value.match(self.encname):
            self.model = 'X Series'
            self.controller = True
        elif self.encname.startswith('ECStream 3U16+4R-4X6G.3'):
            cooling_elements = [v for k, v in self.stat[self.num]['elements'].items() if v['type'] == 3]
            if any(i['descriptor'] == 'SD_9GV12P1J_12R6K4' for i in cooling_elements):
                # z series head-unit uses same enclosure as E16
                # the distinguishing identifier being a cooling element
                self.model = 'Z Series'
                self.controller = True
            else:
                self.model = 'E16'
        elif RE.R.value.match(self.encname) or RE.R20.value.match(self.encname) or RE.R50.value.match(self.encname):
            self.model = self.product.replace('TRUENAS-', '')
            self.controller = True
        elif self.encname == 'AHCI SGPIO Enclosure 2.00':
            if self.product in ['TRUENAS-R20', 'TRUENAS-R20A']:
                self.model = self.product.replace('TRUENAS-', '')
                self.controller = True
            elif RE.MINI.value.match(self.product):
                # TrueNAS Mini's do not have their product name stripped
                self.model = self.product
                self.controller = True
        # enclosure shelf detection
        elif self.encname.startswith('ECStream 3U16RJ-AC.r3'):
            self.model = 'E16'
        elif self.encname.startswith('Storage 1729'):
            self.model = 'E24'
        elif self.encname.startswith('QUANTA JB9 SIM'):
            self.model = 'E60'
        elif self.encname.startswith('CELESTIC X2012'):
            self.model = 'ES12'
        elif RE.ES24.value.match(self.encname):
            self.model = 'ES24'
        elif RE.ES24F.value.match(self.encname):
            self.model = 'ES24F'
        elif self.encname.startswith('CELESTIC R0904'):
            self.model = 'ES60'
        elif self.encname.startswith('HGST H4102-J'):
            self.model = 'ES102'
        else:
            # set to empty string
            self.model = ''

    def _parse_elements(self, elements):
        final = {}
        for slot, element in elements.items():
            try:
                element_type = ELEMENT_TYPES[element['type']]
            except KeyError:
                # means the element type that's being
                # reported to us is unknown so log it
                # and continue on
                logger.warning('Unknown element type: {element["type"]} for {self.devname}')
                continue

            try:
                element_status = ELEMENT_DESC[element['status'][0]]
            except KeyError:
                # means the elements status reported by the enclosure
                # is not mapped so just report unknown
                element_status = 'UNKNOWN'

            if element_type[0] not in final:
                # first time seeing this element type so add it
                final[element_type[0]] = {}

            # convert list of integers representing the elements
            # raw status to an integer so it can be converted
            # appropriately based on the element type
            value_raw = 0
            for val in element['status']:
                value_raw = (value_raw << 8) + val

            parsed = {slot: {
                'descriptor': element['descriptor'],
                'status': element_status,
                'value': element_type[1](value_raw),
                'value_raw': value_raw,
            }}
            if element['descriptor'] == 'Array Device Slot':
                # we always have a 'dev' key that's been strip()'ed,
                # we just need to pull out the da# (if there is one)
                da = [y for y in element['dev'].split(',') if not y.startswith('pass')]
                if da:
                    parsed[slot].update({'dev': da})
                else:
                    parsed[slot].update(element['dev'])

            final[element_type[0]].update(parsed)

        return final
