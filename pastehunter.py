#!/usr/bin/python3

import os
import sys
import yara
import json
import hashlib
import requests
import importlib
import logging
from common import parse_config
from postprocess import post_email
import sys
import asyncio

# Set some logging options
logging.basicConfig(format='%(levelname)s:%(filename)s:%(message)s', level=logging.INFO)
logging.getLogger('requests').setLevel(logging.ERROR)
logging.getLogger('elasticsearch').setLevel(logging.ERROR)

logging.info("Reading Configs")
# Parse the config file
conf = parse_config()

logging.info("Configure Inputs")
input_list = []

for input_type, input_values in conf["inputs"].items():
    if input_values["enabled"]:
        input_list.append(input_values)
        logging.info("Enabled Input: {0}".format(input_type))

logging.info("Configure Outputs")
outputs = []
for output_type, output_values in conf["outputs"].items():
    if output_values["enabled"]:
        logging.info("Enabled Output: {0}".format(output_type))
        _module = importlib.import_module(output_values["module"])
        _class = getattr(_module, output_values["classname"])
        instance = _class()
        outputs.append(instance)


def yara_index(rule_path, blacklist, test_rules):
    index_file = os.path.join(rule_path, 'index.yar')
    with open(index_file, 'w') as yar:
        for filename in os.listdir('YaraRules'):
            if filename.endswith('.yar') and filename != 'index.yar':
                if filename == 'blacklist.yar':
                    if blacklist:
                        logging.info("Enable Blacklist Rules")
                    else:
                        continue
                if filename == 'test_rules.yar':
                    if test_rules:
                        logging.info("Enable Test Rules")
                    else:
                        continue
                include = 'include "{0}"\n'.format(filename)
                yar.write(include)

async def paste_scanner(queue):
    # Get a paste URI from the Queue
    # Fetch the raw paste
    # scan the Paste
    # Store the Paste
    while True:
        paste_data = await queue.get()
        logging.debug("Found New {0} paste {1}".format(paste_data['pastesite'], paste_data['pasteid']))
        raw_paste_uri = paste_data['scrape_url']
        if 'raw_paste' in paste_data:
            raw_paste_data = paste_data['raw_paste']
        else:
            raw_paste_data = requests.get(raw_paste_uri).text

        try:
            # Scan with yara
            matches = rules.match(data=raw_paste_data)
        except Exception as e:
            logging.error("Unable to scan raw paste : {0} - {1}".format(paste_data['pasteid'], e))
            q.task_done()
            continue

        results = []
        for match in matches:
            # For keywords get the word from the matched string
            if match.rule == 'core_keywords' or match.rule == 'custom_keywords':
                for s in match.strings:
                    rule_match = s[1].lstrip('$')
                    if rule_match not in results:
                        results.append(rule_match)
                results.append(str(match.rule))

            # But a break in here for the base64. Will use it later.
            elif match.rule.startswith('b64'):
                results.append(match.rule)

            # Else use the rule name
            else:
                results.append(match.rule)

        # Blacklist Check
        # If any of the blacklist rules appear then empty the result set
        if conf['yara']['blacklist'] and 'blacklist' in results:
            results = []
            logging.info("Blacklisted {0} paste {1}".format(paste_data['pastesite'], paste_data['pasteid']))

        # Post Process

        # If post module is enabled and the paste has a matching rule.
        post_results = paste_data
        for post_process, post_values in conf["post_process"].items():
            if post_values["enabled"]:
                if any(i in results for i in post_values["rule_list"]):
                    logging.info("Running Post Module on {0}".format(paste_data["pasteid"]))
                    post_module = importlib.import_module(post_values["module"])
                    post_results = post_module.run(results,
                                                   raw_paste_data,
                                                   paste_data
                                                   )

        # Throw everything back to paste_data for ease.
        paste_data = post_results


        # If we have a result add some meta data and send to storage
        # If results is empty, ie no match, and store_all is True,
        # then append "no_match" to results. This will then force output.

        #ToDo: Need to make this check for each output not universal

        paste_site = paste_data['pastesite']
        if paste_site == 'pastebin.com' and conf['inputs']['pastebin']['store_all']:
            if len(results) == 0:
                results.append('no_match')

        if len(results) > 0:

            encoded_paste_data = raw_paste_data.encode('utf-8')
            md5 = hashlib.md5(encoded_paste_data).hexdigest()
            sha256 = hashlib.sha256(encoded_paste_data).hexdigest()
            paste_data['MD5'] = md5
            paste_data['SHA256'] = sha256
            paste_data['raw_paste'] = raw_paste_data
            paste_data['YaraRule'] = results
            for output in outputs:
                try:
                    output.store_paste(paste_data)
                except Exception as e:
                    logging.error("Unable to store {0}".format(paste_data["pasteid"]))

        # Mark Tasks as complete
        queue.task_done()

def load_streaming_modules(queue):
    streaming_inputs = []
    for i in input_list:
        if 'streaming' in i and i['streaming']:
            _module = importlib.import_module(i["module"])
            streaming_inputs.append(
                _module.produce(queue)
            )
    return streaming_inputs

def list_pooling_modules():
    for i in input_list:
        if not 'streaming' in i or not i['streaming']:
            yield i['module']

async def paste_pooler(queue):
    logging.info("Populating Queue")
    if os.path.exists('paste_history.tmp'):
        with open('paste_history.tmp') as json_file:
            paste_history = json.load(json_file)
    else:
        paste_history = {}

    # Now Fill the Queue
    while True:
        for input_name in list_pooling_modules():
            if input_name in paste_history:
                input_history = paste_history[input_name]
            else:
                input_history = []
            i = importlib.import_module(input_name)
            # Get list of recent pastes
            logging.info("Fetching paste list from {0}".format(input_name))
            paste_list, history = i.recent_pastes(conf, input_history)
            print (paste_list)
            for paste in paste_list:
                await queue.put(paste)
            paste_history[input_name] = history

        logging.debug("Writing History")
        # Write History
        with open('paste_history.tmp', 'w') as outfile:
            json.dump(paste_history, outfile)

        # Slow it down a little
        logging.info("Sleeping for " + str(conf['general']['run_frequency']) + " Seconds")
        await asyncio.sleep(conf['general']['run_frequency'])


if __name__ == "__main__":
    logging.info("Compile Yara Rules")
    try:
        # Update the yara rules index
        yara_index(conf['yara']['rule_path'],
                   conf['yara']['blacklist'],
                   conf['yara']['test_rules'])

        # Compile the yara rules we will use to match pastes
        index_file = os.path.join(conf['yara']['rule_path'], 'index.yar')
        rules = yara.compile(index_file)
    except Exception as e:
        print("Unable to Create Yara index: ", e)
        sys.exit()

    # Create Queue to hold paste URI's
    queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    inputs = asyncio.gather(*load_streaming_modules(queue))
    scanner = asyncio.gather(paste_scanner(queue))
    pooler = asyncio.gather(paste_pooler(queue))

    try:
        loop.run_until_complete(
            asyncio.gather(
                inputs,
                scanner,
                pooler
            )
        )
    except KeyboardInterrupt:
        logging.info("Stopping Threads")
