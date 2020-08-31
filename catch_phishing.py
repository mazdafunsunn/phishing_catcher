#!/usr/bin/env python
# Copyright (c) 2017 @x0rz
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
import re
import math

import certstream
import tqdm
import yaml
import time
import os
from Levenshtein import distance
from termcolor import colored, cprint
from tld import get_tld

from confusables import unconfuse
import argparse

from datetime import datetime, timezone
import csv

TIMESTAMP_OUTPUT_FORMAT = "%FT%T%z"

CERTSTREAM_URL_DEFAULT = 'wss://certstream.calidog.io'

LOG_SUSPICIOUS_DEFAULT = os.path.dirname(os.path.realpath(__file__))+'/suspicious_domains_'+time.strftime("%Y-%m-%d")+'.log'

SUSPICIOUS_DEFAULT = os.path.dirname(os.path.realpath(__file__))+'/suspicious.yaml'

EXTERNAL_YAML_DEFAULT = os.path.dirname(os.path.realpath(__file__))+'/external.yaml'

WHITELIST_YAML_DEFAULT = os.path.dirname(os.path.realpath(__file__))+'/whitelist.yaml'

def entropy(string):
    """Calculates the Shannon entropy of a string"""
    prob = [ float(string.count(c)) / len(string) for c in dict.fromkeys(list(string)) ]
    entropy = - sum([ p * math.log(p) / math.log(2.0) for p in prob ])
    return entropy

def score_domain(domain):
    """Score `domain`.

    The highest score, the most probable `domain` is a phishing site.

    Args:
        domain (str): the domain to check.

    Returns:
        int: the score of `domain`.
    """
    score = 0

    for t in suspicious['whitelist']:
        if domain.endswith(t):
            # this should already be set to 0!
            return score

    for t in suspicious['tlds']:
        if domain.endswith(t):
            score += 20

    # Remove initial '*.' for wildcard certificates bug
    if domain.startswith('*.'):
        domain = domain[2:]

    # Removing TLD to catch inner TLD in subdomain (ie. paypal.com.domain.com)
    try:
        res = get_tld(domain, as_object=True, fail_silently=True, fix_protocol=True)
        domain = '.'.join([res.subdomain, res.domain])
    except Exception:
        pass

    # Higer entropy is kind of suspicious
    score += int(round(entropy(domain)*10))

    # Remove lookalike characters using list from http://www.unicode.org/reports/tr39
    domain = unconfuse(domain)

    words_in_domain = re.split(r"\W+", domain)

    # ie. detect fake .com (ie. *.com-account-management.info)
    if words_in_domain[0] in ['com', 'net', 'org']:
        score += 10

    # Testing keywords
    for word in suspicious['keywords']:
        if word in domain:
            score += suspicious['keywords'][word]

    # Testing Levenshtein distance for strong keywords (>= 70 points) (ie. paypol)
    for key in [k for (k,s) in suspicious['keywords'].items() if s >= 70]:
        # Removing too generic keywords (ie. mail.domain.com)
        for word in [w for w in words_in_domain if w not in ['email', 'mail', 'cloud']]:
            if distance(str(word), str(key)) == 1:
                score += 70

    # Lots of '-' (ie. www.paypal-datacenter.com-acccount-alert.com)
    if 'xn--' not in domain and domain.count('-') >= 4:
        score += domain.count('-') * 3

    # Deeply nested subdomains (ie. www.paypal.com.security.accountupdate.gq)
    if domain.count('.') >= 3:
        score += domain.count('.') * 3

    return score


def callback(message, context):
    """Callback handler for certstream events."""
    if message['message_type'] == "heartbeat":
        return

    if message['message_type'] == "certificate_update":
        all_domains = message['data']['leaf_cert']['all_domains']

        for domain in all_domains:
            pbar.update(1)
            score = score_domain(domain.lower())

            # If issued from a free CA = more suspicious
            if "Let's Encrypt" in message['data']['chain'][0]['subject']['aggregated']:
                score += 10

            if score >= 100:
                tqdm.tqdm.write(
                    "[!] Suspicious: "
                    "{} (score={})".format(colored(domain, 'red', attrs=['underline', 'bold']), score))
            elif score >= 90:
                tqdm.tqdm.write(
                    "[!] Suspicious: "
                    "{} (score={})".format(colored(domain, 'red', attrs=['underline']), score))
            elif score >= 80:
                tqdm.tqdm.write(
                    "[!] Likely    : "
                    "{} (score={})".format(colored(domain, 'yellow', attrs=['underline']), score))
            elif score >= 65:
                tqdm.tqdm.write(
                    "[+] Potential : "
                    "{} (score={})".format(colored(domain, attrs=['underline']), score))

            if score >= 75:
                if not args.details:
                    suspicious_writer.writerow(
                        [
                            domain
                        ]
                    )
                else:
                    suspicious_writer.writerow(
                        [
                            datetime.now(timezone.utc).strftime(TIMESTAMP_OUTPUT_FORMAT),
                            domain,
                            score,
                            "|".join(message["data"]["leaf_cert"]["all_domains"]),
                            message["data"]["leaf_cert"]["fingerprint"],
                            message["data"]["leaf_cert"]["serial_number"],
                            datetime.fromtimestamp(message["data"]["leaf_cert"]["not_before"], tz=timezone.utc).strftime(TIMESTAMP_OUTPUT_FORMAT),
                            datetime.fromtimestamp(message["data"]["leaf_cert"]["not_after"], tz=timezone.utc).strftime(TIMESTAMP_OUTPUT_FORMAT),
                            message["data"]["leaf_cert"]["subject"]["aggregated"],
                            datetime.fromtimestamp(message["data"]["seen"], tz=timezone.utc).strftime(TIMESTAMP_OUTPUT_FORMAT),
                            message["data"]["source"]["name"],
                            message["data"]["source"]["url"],
                            message["data"]["update_type"]
                        ]
                    )
                suspicious_file.flush()

if __name__ == '__main__':

    parser = argparse.ArgumentParser(
        description="Identify suspicious domains from Certificate Transparency Logs."
    )

    parser.add_argument(
        '--debug', '-d',
        action='store_true',
        default=False,
        help='Enable debugging output.'
    )

    parser.add_argument(
        '--certstream-url', '-u',
        type=str,
        default=CERTSTREAM_URL_DEFAULT,
        help=f"URL for the Certificate Transparency stream. DEFAULT: {CERTSTREAM_URL_DEFAULT}."
    )

    parser.add_argument(
        '--suspicious-path', '-s',
        type=str,
        default=LOG_SUSPICIOUS_DEFAULT,
        help=f'File in which to store the suspicious domain log. DEFAULT: {LOG_SUSPICIOUS_DEFAULT}.'
    )

    parser.add_argument(
        '--suspicious-yaml', '-S',
        type=str,
        default=SUSPICIOUS_DEFAULT,
        help=f'YAML file containing suspicious entries and weights. DEFAULT: {SUSPICIOUS_DEFAULT}.'
    )

    parser.add_argument(
        '--external-yaml', '-E',
        type=str,
        default=EXTERNAL_YAML_DEFAULT,
        help=f'YAML file containing site-specific suspicious entries and weights. DEFAULT: {EXTERNAL_YAML_DEFAULT}.'
    )

    parser.add_argument(
        '--details', '-D',
        #type=bool,
        action='store_true',
        default=False,
        help='Add more details to the suspicious domain log. DEFAULT: False.'
    )

    args = parser.parse_args()

    if args.debug:
        print("**********")
        print(f"Command line args: {args}")
        print("**********")
        print()

    pbar = tqdm.tqdm(desc='certificate_update', unit=' certs')

    with open(args.suspicious_yaml, 'r') as f:
        suspicious = yaml.safe_load(f)

    with open(args.external_yaml, 'r') as f:
        external = yaml.safe_load(f)

    if external['override_suspicious.yaml'] is True:
        suspicious = external
    else:
        if external['keywords'] is not None:
            suspicious['keywords'].update(external['keywords'])

        if external['tlds'] is not None:
            suspicious['tlds'].update(external['tlds'])
    
    # Open the suspicious domain file for writing only once. The
    # callback will also access this globally.
    log_suspicious = args.suspicious_path
    suspicious_file = open(log_suspicious, 'a')
    suspicious_writer = csv.writer(
        suspicious_file,
        dialect='excel'
    )

    suspicious_writer.writerow(
        [
            "timestamp",
            "domain",
            "score",
            "all_domains",
            "fingerprint",
            "serial_number",
            "not_before",
            "not_after",
            "subject",
            "seen",
            "issuer",
            "issuer_url",
            "message_type"
        ]
    )

    certstream.listen_for_events(callback, url=args.certstream_url)
