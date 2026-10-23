"""Fixed region -> country lists for the geographic filter (docs/BUILD_PLAN.md section 4c).

The lists live in the repo, not in a web service, so the same profile always expands to the
same ISO 3166-1 alpha-2 codes. Change a list only together with a new REGIONS_VERSION; every
run records the version it used.

Assignment rule: each country is in exactly one region. Where a country could be in two, the
first line that applies decides:
  1. Mexico, Central America, South America and the Caribbean (including territories such as
     Puerto Rico and the Cayman Islands) -> Latin America and Caribbean.
  2. Only the United States, Canada, Bermuda, Greenland and Saint-Pierre-et-Miquelon ->
     North America.
  3. The Arab League states, Iran, Israel, Palestine, Turkey and Western Sahara, plus Sudan
     and Djibouti -> Middle East and North Africa. So Egypt is MENA, not Africa, and Turkey is
     MENA, not Europe or Asia. Mauritania, Somalia and the Comoros -> Africa.
  4. Russia (the whole country) and Cyprus -> Europe. Armenia, Azerbaijan, Georgia and the
     Central Asian states, including Kazakhstan -> Asia.
  5. Remaining African states and territories -> Africa; remaining Asian ones -> Asia;
     Australia, New Zealand and the Pacific islands (including the Australian external
     territories) -> Oceania. The British Indian Ocean Territory -> Africa.
Not assigned to any region (no resident research community): Antarctica (AQ), Bouvet Island
(BV), Heard and McDonald Islands (HM), French Southern Territories (TF), South Georgia (GS)
and US Minor Outlying Islands (UM). Kosovo has no ISO code and is not listed.
"""

REGIONS_VERSION = "2026-10-v1"
GEO_BASES = ("author_affiliation",)
GEO_MODES = ("include", "exclude")


def _names(text: str) -> dict[str, str]:
    return dict(item.split("=", 1) for item in text.split(";"))


_DEFINITIONS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("north_america", "North America", _names(
        "BM=Bermuda;CA=Canada;GL=Greenland;PM=Saint Pierre and Miquelon;US=United States")),
    ("latin_america_caribbean", "Latin America and Caribbean", _names(
        "AG=Antigua and Barbuda;AI=Anguilla;AR=Argentina;AW=Aruba;BB=Barbados;"
        "BL=Saint Barthélemy;BO=Bolivia;BQ=Bonaire, Sint Eustatius and Saba;BR=Brazil;"
        "BS=Bahamas;BZ=Belize;CL=Chile;CO=Colombia;CR=Costa Rica;CU=Cuba;CW=Curaçao;"
        "DM=Dominica;DO=Dominican Republic;EC=Ecuador;FK=Falkland Islands;GD=Grenada;"
        "GF=French Guiana;GP=Guadeloupe;GT=Guatemala;GY=Guyana;HN=Honduras;HT=Haiti;"
        "JM=Jamaica;KN=Saint Kitts and Nevis;KY=Cayman Islands;LC=Saint Lucia;"
        "MF=Saint Martin (French part);MQ=Martinique;MS=Montserrat;MX=Mexico;"
        "NI=Nicaragua;PA=Panama;PE=Peru;PR=Puerto Rico;PY=Paraguay;SR=Suriname;"
        "SV=El Salvador;SX=Sint Maarten (Dutch part);TC=Turks and Caicos Islands;"
        "TT=Trinidad and Tobago;UY=Uruguay;VC=Saint Vincent and the Grenadines;"
        "VE=Venezuela;VG=British Virgin Islands;VI=U.S. Virgin Islands")),
    ("europe", "Europe", _names(
        "AD=Andorra;AL=Albania;AT=Austria;AX=Åland Islands;BA=Bosnia and Herzegovina;"
        "BE=Belgium;BG=Bulgaria;BY=Belarus;CH=Switzerland;CY=Cyprus;CZ=Czechia;"
        "DE=Germany;DK=Denmark;EE=Estonia;ES=Spain;FI=Finland;FO=Faroe Islands;"
        "FR=France;GB=United Kingdom;GG=Guernsey;GI=Gibraltar;GR=Greece;HR=Croatia;"
        "HU=Hungary;IE=Ireland;IM=Isle of Man;IS=Iceland;IT=Italy;JE=Jersey;"
        "LI=Liechtenstein;LT=Lithuania;LU=Luxembourg;LV=Latvia;MC=Monaco;MD=Moldova;"
        "ME=Montenegro;MK=North Macedonia;MT=Malta;NL=Netherlands;NO=Norway;PL=Poland;"
        "PT=Portugal;RO=Romania;RS=Serbia;RU=Russia;SE=Sweden;SI=Slovenia;"
        "SJ=Svalbard and Jan Mayen;SK=Slovakia;SM=San Marino;UA=Ukraine;"
        "VA=Vatican City")),
    ("africa", "Africa", _names(
        "AO=Angola;BF=Burkina Faso;BI=Burundi;BJ=Benin;BW=Botswana;"
        "CD=Democratic Republic of the Congo;CF=Central African Republic;CG=Congo;"
        "CI=Côte d'Ivoire;CM=Cameroon;CV=Cabo Verde;ER=Eritrea;ET=Ethiopia;GA=Gabon;"
        "GH=Ghana;GM=Gambia;GN=Guinea;GQ=Equatorial Guinea;GW=Guinea-Bissau;"
        "IO=British Indian Ocean Territory;KE=Kenya;KM=Comoros;LR=Liberia;LS=Lesotho;"
        "MG=Madagascar;ML=Mali;MR=Mauritania;MU=Mauritius;MW=Malawi;MZ=Mozambique;"
        "NA=Namibia;NE=Niger;NG=Nigeria;RE=Réunion;RW=Rwanda;SC=Seychelles;"
        "SH=Saint Helena, Ascension and Tristan da Cunha;SL=Sierra Leone;SN=Senegal;"
        "SO=Somalia;SS=South Sudan;ST=São Tomé and Príncipe;SZ=Eswatini;TD=Chad;"
        "TG=Togo;TZ=Tanzania;UG=Uganda;YT=Mayotte;ZA=South Africa;ZM=Zambia;"
        "ZW=Zimbabwe")),
    ("middle_east_north_africa", "Middle East and North Africa", _names(
        "AE=United Arab Emirates;BH=Bahrain;DJ=Djibouti;DZ=Algeria;EG=Egypt;"
        "EH=Western Sahara;IL=Israel;IQ=Iraq;IR=Iran;JO=Jordan;KW=Kuwait;LB=Lebanon;"
        "LY=Libya;MA=Morocco;OM=Oman;PS=Palestine;QA=Qatar;SA=Saudi Arabia;SD=Sudan;"
        "SY=Syria;TN=Tunisia;TR=Türkiye;YE=Yemen")),
    ("asia", "Asia", _names(
        "AF=Afghanistan;AM=Armenia;AZ=Azerbaijan;BD=Bangladesh;BN=Brunei;BT=Bhutan;"
        "CN=China;GE=Georgia;HK=Hong Kong;ID=Indonesia;IN=India;JP=Japan;"
        "KG=Kyrgyzstan;KH=Cambodia;KP=North Korea;KR=South Korea;KZ=Kazakhstan;"
        "LA=Laos;LK=Sri Lanka;MM=Myanmar;MN=Mongolia;MO=Macao;MV=Maldives;"
        "MY=Malaysia;NP=Nepal;PH=Philippines;PK=Pakistan;SG=Singapore;TH=Thailand;"
        "TJ=Tajikistan;TL=Timor-Leste;TM=Turkmenistan;TW=Taiwan;UZ=Uzbekistan;"
        "VN=Vietnam")),
    ("oceania", "Oceania", _names(
        "AS=American Samoa;AU=Australia;CC=Cocos (Keeling) Islands;CK=Cook Islands;"
        "CX=Christmas Island;FJ=Fiji;FM=Micronesia;GU=Guam;KI=Kiribati;"
        "MH=Marshall Islands;MP=Northern Mariana Islands;NC=New Caledonia;"
        "NF=Norfolk Island;NR=Nauru;NU=Niue;NZ=New Zealand;PF=French Polynesia;"
        "PG=Papua New Guinea;PN=Pitcairn Islands;PW=Palau;SB=Solomon Islands;"
        "TK=Tokelau;TO=Tonga;TV=Tuvalu;VU=Vanuatu;WF=Wallis and Futuna;WS=Samoa")),
)

# region id -> display name; region id -> sorted tuple of codes; code -> English name.
REGION_NAMES: dict[str, str] = {rid: name for rid, name, _ in _DEFINITIONS}
REGIONS: dict[str, tuple[str, ...]] = {
    rid: tuple(sorted(countries)) for rid, _, countries in _DEFINITIONS}
COUNTRY_NAMES: dict[str, str] = dict(sorted(
    (code, name) for _, _, countries in _DEFINITIONS for code, name in countries.items()))
COUNTRY_REGION: dict[str, str] = {
    code: rid for rid, codes in REGIONS.items() for code in codes}


def expand(regions: list[str], countries: list[str]) -> list[str]:
    """Chosen regions plus explicit countries -> one sorted list of unique codes."""
    codes = set(countries)
    for rid in regions:
        codes.update(REGIONS[rid])
    return sorted(codes)


def regions_of(countries: list[str]) -> list[str]:
    """Region ids (in the fixed region order) that contain any of the given countries."""
    found = {COUNTRY_REGION[c] for c in countries if c in COUNTRY_REGION}
    return [rid for rid in REGIONS if rid in found]


def regions_payload() -> dict:
    return {
        "version": REGIONS_VERSION,
        "bases": list(GEO_BASES),
        "modes": list(GEO_MODES),
        "regions": [{"id": rid, "name": REGION_NAMES[rid], "countries": list(codes)}
                    for rid, codes in REGIONS.items()],
        "countries": [{"code": c, "name": n, "region": COUNTRY_REGION[c]}
                      for c, n in COUNTRY_NAMES.items()],
    }
