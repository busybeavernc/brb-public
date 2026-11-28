"""Sales tax at checkout: North Carolina buyers pay their county's combined rate (state 4.75% + county + transit);
everyone else pays none (BRB has no sales-tax duty elsewhere until it passes another state's threshold - the admin
"Sales by state" list shows that coming).

Off until switched on in Admin > Pricing (SALES_TAX). County rates: NCDOR's "Current Sales and Use Tax Rates",
effective July 1, 2026; when NCDOR changes one, paste the new rate in Admin (NC_TAX_RATES, "County: 7.25" lines).
ZIP -> county: the US Postal Service's primary county for each NC ZIP. A ZIP that crosses a county line can be wrong,
so the buyer confirms the county.
"""
import json
import os
from decimal import ROUND_HALF_UP, Decimal

from . import settings

# NCDOR, Current Sales and Use Tax Rates, effective 7-1-2026 (Durham, Mecklenburg, Orange, Wake include transit tax)
NC_RATES = {
    'Alamance': '6.75', 'Alexander': '7', 'Alleghany': '7', 'Anson': '7', 'Ashe': '7', 'Avery': '6.75',
    'Beaufort': '6.75', 'Bertie': '7', 'Bladen': '6.75', 'Brunswick': '6.75', 'Buncombe': '7', 'Burke': '6.75',
    'Cabarrus': '7', 'Caldwell': '6.75', 'Camden': '6.75', 'Carteret': '6.75', 'Caswell': '6.75', 'Catawba': '7',
    'Chatham': '7', 'Cherokee': '7', 'Chowan': '6.75', 'Clay': '7', 'Cleveland': '6.75', 'Columbus': '6.75',
    'Craven': '6.75', 'Cumberland': '7', 'Currituck': '6.75', 'Dare': '6.75', 'Davidson': '7', 'Davie': '6.75',
    'Duplin': '7', 'Durham': '7.5', 'Edgecombe': '7', 'Forsyth': '7', 'Franklin': '6.75', 'Gaston': '7',
    'Gates': '6.75', 'Graham': '7', 'Granville': '6.75', 'Greene': '7', 'Guilford': '6.75', 'Halifax': '7',
    'Harnett': '7', 'Haywood': '7', 'Henderson': '6.75', 'Hertford': '7', 'Hoke': '6.75', 'Hyde': '6.75',
    'Iredell': '6.75', 'Jackson': '7', 'Johnston': '6.75', 'Jones': '7', 'Lee': '7', 'Lenoir': '6.75', 'Lincoln': '7',
    'Macon': '6.75', 'Madison': '7', 'Martin': '7', 'McDowell': '6.75', 'Mecklenburg': '8.25', 'Mitchell': '6.75',
    'Montgomery': '7', 'Moore': '7', 'Nash': '6.75', 'New Hanover': '7', 'Northampton': '6.75', 'Onslow': '7',
    'Orange': '7.5', 'Pamlico': '6.75', 'Pasquotank': '7', 'Pender': '6.75', 'Perquimans': '6.75', 'Person': '6.75',
    'Pitt': '7', 'Polk': '6.75', 'Randolph': '7', 'Richmond': '6.75', 'Robeson': '7', 'Rockingham': '7', 'Rowan': '7',
    'Rutherford': '7', 'Sampson': '7', 'Scotland': '6.75', 'Stanly': '7', 'Stokes': '6.75', 'Surry': '7', 'Swain': '7',
    'Transylvania': '6.75', 'Tyrrell': '6.75', 'Union': '6.75', 'Vance': '6.75', 'Wake': '7.25', 'Warren': '6.75',
    'Washington': '7', 'Watauga': '6.75', 'Wayne': '6.75', 'Wilkes': '7', 'Wilson': '6.75', 'Yadkin': '6.75',
    'Yancey': '6.75',
}
RATES_AS_OF = 'July 1, 2026'

STATES = [('AL', 'Alabama'), ('AK', 'Alaska'), ('AZ', 'Arizona'), ('AR', 'Arkansas'), ('CA', 'California'),
          ('CO', 'Colorado'), ('CT', 'Connecticut'), ('DE', 'Delaware'), ('DC', 'District of Columbia'),
          ('FL', 'Florida'), ('GA', 'Georgia'), ('HI', 'Hawaii'), ('ID', 'Idaho'), ('IL', 'Illinois'), ('IN', 'Indiana'),
          ('IA', 'Iowa'), ('KS', 'Kansas'), ('KY', 'Kentucky'), ('LA', 'Louisiana'), ('ME', 'Maine'), ('MD', 'Maryland'),
          ('MA', 'Massachusetts'), ('MI', 'Michigan'), ('MN', 'Minnesota'), ('MS', 'Mississippi'), ('MO', 'Missouri'),
          ('MT', 'Montana'), ('NE', 'Nebraska'), ('NV', 'Nevada'), ('NH', 'New Hampshire'), ('NJ', 'New Jersey'),
          ('NM', 'New Mexico'), ('NY', 'New York'), ('NC', 'North Carolina'), ('ND', 'North Dakota'), ('OH', 'Ohio'),
          ('OK', 'Oklahoma'), ('OR', 'Oregon'), ('PA', 'Pennsylvania'), ('PR', 'Puerto Rico'), ('RI', 'Rhode Island'),
          ('SC', 'South Carolina'), ('SD', 'South Dakota'), ('TN', 'Tennessee'), ('TX', 'Texas'), ('UT', 'Utah'),
          ('VT', 'Vermont'), ('VA', 'Virginia'), ('WA', 'Washington'), ('WV', 'West Virginia'), ('WI', 'Wisconsin'),
          ('WY', 'Wyoming'), ('XX', 'Outside the United States')]
STATE_NAMES = dict(STATES)
_zips = {}


def enabled():
    return str(settings.get('SALES_TAX', '') or '').lower() in ('1', 'on', 'true', 'yes')


def zip_counties():
    if not _zips:
        with open(os.path.join(os.path.dirname(__file__), 'nc_zip_counties.json'), encoding='utf-8') as f:
            _zips.update(json.load(f))
    return _zips


def rates():
    """County -> rate (percent, as text), with any rates changed in Admin."""
    out = dict(NC_RATES)
    for line in str(settings.get('NC_TAX_RATES', '') or '').splitlines():
        name, _, val = line.partition(':')
        name = _county_name(name)
        try:
            v = Decimal(val.strip().rstrip('%'))
        except ArithmeticError:
            continue
        if name and 0 <= v < 20:
            out[name] = format(v.normalize(), 'f')
    return out


def parse_rates(text):
    """Admin's rates box -> the cleaned text to save. Raises ValueError on a line it can't read."""
    lines = []
    for raw in str(text or '').splitlines():
        if not raw.strip():
            continue
        name, sep, val = raw.partition(':')
        county = _county_name(name)
        try:
            v = Decimal(val.strip().rstrip('%')) if sep else None
        except ArithmeticError:
            v = None
        if not county or v is None or not 0 <= v < 20:
            raise ValueError(f'Sales tax rates: "{raw.strip()}" should look like "Wake: 7.25" (an NC county).')
        lines.append(f'{county}: {format(v.normalize(), "f")}')
    return '\n'.join(lines)


def _county_name(name):
    n = str(name or '').strip().removesuffix(' County').removesuffix(' county').strip().lower()
    for c in NC_RATES:
        if c.lower() == n:
            return c
    return None


def county_for_zip(zipcode):
    return zip_counties().get(str(zipcode or '').strip()[:5])


def quote(state, zipcode='', county=''):
    """Where the buyer is -> {'state', 'zip', 'county', 'rate' (Decimal percent), 'label'} or raises ValueError."""
    state = str(state or '').strip().upper()
    if not state and not enabled():   # only needed for tax; the form still asks (for the sales-by-state list)
        return {'state': '', 'zip': '', 'county': '', 'rate': Decimal('0'), 'label': ''}
    if state not in STATE_NAMES:
        raise ValueError('Choose where you live (for sales tax).')
    zipcode = ''.join(ch for ch in str(zipcode or '') if ch.isdigit())[:5]
    out = {'state': state, 'zip': zipcode, 'county': '', 'rate': Decimal('0'), 'label': ''}
    if state != 'NC' or not enabled():
        return out
    if len(zipcode) != 5:
        raise ValueError('Enter your 5-digit ZIP code (North Carolina charges sales tax by county).')
    c = _county_name(county) or county_for_zip(zipcode)
    if not c:
        raise ValueError("That ZIP code isn't in North Carolina. Check it, or choose your state again.")
    rate = Decimal(rates()[c])
    out.update(county=c, rate=rate, label=f'NC sales tax ({c} County, {format(rate.normalize(), "f")}%)')
    return out


def tax_cents(amount_cents, rate):
    return int((Decimal(amount_cents) * Decimal(rate) / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def page_data():
    """For the checkout form's script: ZIP -> county, county -> rate."""
    return {'on': enabled(), 'zips': zip_counties() if enabled() else {}, 'rates': rates() if enabled() else {}}
