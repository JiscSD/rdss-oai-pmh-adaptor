#!/usr/bin/env python3
import json
import logging
import os
import sys
import itertools

from app import EPrintsClient
from app import DownloadClient
from app import DynamoDBClient
from app import KinesisClient
from app import MessageGenerator
from app import MessageValidator
from app import PoisonPill
from app import S3Client
from datetime import datetime

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s [%(threadName)s] [%(levelname)s] %(name)s - %(message)s'
)

download_client = None
dynamodb_client = None
eprints_client = None
kinesis_client = None
message_generator = None
message_validator = None
s3_client = None


def main():
    # Fetch the application settings.
    settings = _get_settings()

    # Initialise the various clients, generator, etc.
    global download_client
    download_client = _initialise_download_client()
    global dynamodb_client
    dynamodb_client = _initialise_dynamodb_client(settings)
    global eprints_client
    eprints_client = _initialise_eprints_client(settings)
    global kinesis_client
    kinesis_client = _initialise_kinesis_client(settings)
    global message_generator
    message_generator = _initialise_message_generator(settings)
    global message_validator
    message_validator = _initialise_message_validator(settings)
    global s3_client
    s3_client = _initialise_s3_client(settings)

    # Query DynamoDB for the high watermark. If it exists, use that, otherwise this is probably a
    # "first run", so set the watermark to now.
    start_timestamp = dynamodb_client.fetch_high_watermark()
    if start_timestamp is None:
        start_timestamp = datetime.now()
        dynamodb_client.update_high_watermark(start_timestamp)

    flow_limit = int(settings['EPRINTS_FLOW_LIMIT'])

    # Query EPrints for all the records since the high watermark.
    records = eprints_client.fetch_records_from(start_timestamp)
    # Filter out records that have already been successfully processed
    filtered_records = itertools.islice(filter(_record_success_filter, records), flow_limit)

    for record in filtered_records:
        logging.info('Processing EPrints record [%s]', record)
        _process_record(record)

    # We're done, shut down
    _shutdown()


def _initialise_download_client():
    return DownloadClient()


def _initialise_dynamodb_client(settings):
    return DynamoDBClient(
        settings['EPRINTS_DYNAMODB_WATERMARK_TABLE_NAME'],
        settings['EPRINTS_DYNAMODB_PROCESSED_TABLE_NAME']
    )


def _initialise_eprints_client(settings):
    return EPrintsClient(settings['EPRINTS_EPRINTS_URL'])


def _initialise_kinesis_client(settings):
    return KinesisClient(
        settings['EPRINTS_OUTPUT_KINESIS_STREAM_NAME'],
        settings['EPRINTS_OUTPUT_KINESIS_INVALID_STREAM_NAME']
    )


def _initialise_message_generator(settings):
    return MessageGenerator(settings['EPRINTS_JISC_ID'], settings['EPRINTS_ORGANISATION_NAME'])


def _initialise_message_validator(settings):
    return MessageValidator(settings['EPRINTS_API_SPECIFICATION_VERSION'])


def _initialise_s3_client(settings):
    return S3Client(settings['EPRINTS_S3_BUCKET_NAME'])


def _record_success_filter(record):
    """ Filters out records that have already been processed successfully.
        """
    eprints_identifier = record['header']['identifier']
    status = dynamodb_client.fetch_processed_status(eprints_identifier)
    logging.info(
        'Got processed status [%s] for EPrints identifier [%s]',
        status,
        eprints_identifier
    )
    if status == 'Success':
        logging.info(
            'EPrints record [%s] already successfully processed, skipping',
            eprints_identifier
        )
        return False
    else:
        return True


def _process_record(record):
    eprints_identifier = record['header']['identifier']
    logging.info('Processing EPrints record [%s]', eprints_identifier)
    message, status, reason, err_code = None, 'Success', '-', None
    try:
        # Fetch from EPrints and push the files associated with the record into S3.
        s3_objects = _push_files_to_s3(record)

        # Generate the RDSS compliant message from the EPrints record.
        message = message_generator.generate_metadata_create(record, s3_objects)

        try:
            # Convert the message into a JSON payload and back again
            message = json.dumps(json.loads(message, strict=False))
        except Exception:
            err_code = 'GENERR007'
            raise

        try:
            # Belts and braces check to make sure the message is valid
            message_validator.validate_message(message)
        except Exception:
            err_code = 'GENERR001'
            raise

        # Put the RDSS message onto the message queue.
        kinesis_client.put_message_on_queue(message)

    except Exception as e:
        logging.exception('An error occurred processing EPrints record [%s]', record)
        if err_code is None:
            err_code = 'GENERR009'
        status, reason = 'Failure', str(e)
        message = _decorate_message_with_error(message, err_code, reason)
        kinesis_client.put_invalid_message_on_queue(message)

    # Update the DynamoDB table with the status of the processing of this record.
    dynamodb_client.update_processed_record(
        record['header']['identifier'],
        message if message is not None and len(message) > 0 else '-',
        status,
        reason
    )

    # Update the high watermark to the datestamp of this EPrints record.
    dynamodb_client.update_high_watermark(record['header']['datestamp'])


def _push_files_to_s3(record):
    file_locations = []

    # Iterate over the metadata identifiers. Some of these are just bits and pieces of text, but
    # often it is a file associated with the record.
    identifiers = record['metadata']['identifier']
    for identifier in identifiers:

        # Make sure it's actually a file URL we're looking at...
        if identifier.startswith('http://') or identifier.startswith('https://'):

            # Fetch the file from EPrints, and then push that file into S3 and remove the
            # downloaded file from the disk.
            file_path = download_client.download_file(identifier)
            if file_path is not None:
                file_locations.append(
                    s3_client.push_to_bucket(identifier, file_path)
                )
                try:
                    os.remove(file_path)
                except FileNotFoundError:
                    logging.warning('An error occurred removing file [%s]', file_path)
            else:
                logging.warning('Unable to download EPrints file [%s], skipping file', identifier)
    return file_locations


def _decorate_message_with_error(message, error_code, error_message):
    # We need to be able to get the message as a dict
    try:
        message = json.loads(message, strict=False)
    except Exception:
        logging.warning(
            'Unable to decorate message [%s] with error code [%s] and message [%s]',
            message,
            error_code,
            error_message
        )
        return message

    # Belts and braces - make sure the top level 'messageHeader' exists
    if 'messageHeader' not in message:
        message['messageHeader'] = {}

    # Add the error code and error message to the message
    message['messageHeader']['errorCode'] = error_code
    message['messageHeader']['errorMessage'] = json.dumps(error_message)

    return json.dumps(message)


def _parse_env_vars(env_var_names):
    env_vars = {name: os.environ.get(name) for name in env_var_names}
    if not all(env_vars.values()):
        missing = (name for name, exists in env_vars.items() if not exists)
        logging.error(
            'The following environment variables have not been set: [%s]',
            ', '.join(missing)
        )
        sys.exit(1)
    return env_vars


def _get_settings():
    return _parse_env_vars((
        'EPRINTS_JISC_ID',
        'EPRINTS_ORGANISATION_NAME',
        'EPRINTS_EPRINTS_URL',
        'EPRINTS_DYNAMODB_WATERMARK_TABLE_NAME',
        'EPRINTS_DYNAMODB_PROCESSED_TABLE_NAME',
        'EPRINTS_S3_BUCKET_NAME',
        'EPRINTS_OUTPUT_KINESIS_STREAM_NAME',
        'EPRINTS_OUTPUT_KINESIS_INVALID_STREAM_NAME',
        'EPRINTS_API_SPECIFICATION_VERSION',
        'EPRINTS_FLOW_LIMIT'
    ))


def _shutdown():
    logging.info('Shutting adaptor down...')
    if kinesis_client is not None:
        kinesis_client.put_message_on_queue(PoisonPill)
    if message_validator is not None:
        message_validator.shutdown()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logging.exception('An unhandled error occurred in the main thread')
        _shutdown()
