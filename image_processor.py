import boto
import json
import time
import sys
import getopt
import argparse
import os
import re
import logging
import StringIO
import uuid
import math
import httplib
import multiprocessing
from subprocess import call
import shutil
from boto.sqs.message import RawMessage
from boto.sqs.message import Message
from boto.s3.key import Key

RETRY_COUNT = 3

def process_jobs(queue, s3_output_bucket, s3_endpoint, input_queue, output_queue):
	while True:
		raw_message = queue.get()

		info_message("Message received")
		# Parse JSON message (going two levels deep to get the embedded message)
		message = raw_message.get_body()

		# Create a unique job id
		job_id = str(uuid.uuid4())

		# Process the image, creating the image montage
		output_url = process_message(message, s3_output_bucket, s3_endpoint, job_id)

		if output_url is None:
			info_message("output url was none :( moving on.")
			continue

		output_message = "Output available at: %s" % (output_url)
	
		# Write message to output queue
		write_output_message(output_message, output_queue)
	
		info_message(output_message)
		info_message("Image processing completed.")
	
		# Delete message from the queue
		input_queue.delete_message(raw_message)

		# delete job directory
		clean_up_job(job_id)

##########################################################
# Connect to SQS and poll for messages
##########################################################
def main(argv=None):
	# Handle command-line arguments for AWS credentials and resource names
	parser = argparse.ArgumentParser(description='Process AWS resources and credentials.')
	parser.add_argument('--input-queue', action='store', dest='input_queue', required=False, default="input", help='SQS queue from which input jobs are retrieved')
	parser.add_argument('--output-queue', action='store', dest='output_queue', required=False, default="output", help='SQS queue to which job results are placed')
	parser.add_argument('--s3-output-bucket', action='store', dest='s3_output_bucket', required=False, default="", help='S3 bucket where list of instances will be stored')
	parser.add_argument('--region', action='store', dest='region', required=False, default="", help='Region that the SQS queus are in')
	args = parser.parse_args()

	# Get region
	region_name = args.region

	# If no region supplied, extract it from meta-data
	if region_name == '':
		conn = httplib.HTTPConnection("169.254.169.254", 80)
		conn.request("GET", "/latest/meta-data/placement/availability-zone/")
		response = conn.getresponse()
		region_name = response.read()[:-1]
	info_message('Using Region %s' % (region_name))

	# Set queue names
	input_queue_name = args.input_queue
	output_queue_name = args.output_queue

	# Get S3 endpoint
	s3_endpoint = [region.endpoint for region in boto.s3.regions() if region.name == region_name][0]

	# Get S3 bucket, create if none supplied
	s3_output_bucket = args.s3_output_bucket
	if s3_output_bucket == "":
		s3_output_bucket = create_s3_output_bucket(s3_output_bucket, s3_endpoint, region_name)
	
	info_message('Retrieving jobs from queue %s. Processed images will be stored in %s and a message placed in queue %s' % (input_queue_name, s3_output_bucket, output_queue_name))

	error_count = 0
	def get_sqs_connection(region_name, error_count):
		try:
			# Connect to SQS and open queue
			return boto.sqs.connect_to_region(region_name)
		except Exception as ex:
			if error_count > RETRY_COUNT:
				error_message("Encountered an error setting SQS region.  Please confirm you have queues in %s." % (region_name))
				sys.exit(1)
			else:
				error_count += 1
				time.sleep(5)
				return get_sqs_connection(region_name, error_count)

	def get_queue(sqs, queue_name, error_count):
		try:
			if sqs.lookup(queue_name):
				queue = sqs.get_queue(queue_name)
				queue.set_message_class(RawMessage)
				return queue
			else:
				sqs.create_queue(queue_name)
				return get_queue(sqs, queue_name, error_count)


		except Exception as ex:
			if error_count > RETRY_COUNT:
				error_message("Encountered an error connecting to SQS queue %s." % (queue_name))
				error_message(ex)
				sys.exit(2)
			else:
				error_count += 1
				time.sleep(5)
				return get_queue(sqs, queue_name, error_count)

	sqs = get_sqs_connection(region_name, error_count)
	input_queue = get_queue(sqs, input_queue_name, error_count)
	output_queue = get_queue(sqs, output_queue_name, error_count)

	# start worker process
	queue = multiprocessing.Queue()
	worker = multiprocessing.Process(target=process_jobs, args=(queue, s3_output_bucket, s3_endpoint, input_queue, output_queue))
	worker.start()

	info_message("Polling input queue...")

	while True:
		# Get messages
		rs = input_queue.get_messages(num_messages=1)
	
		if len(rs) > 0:
			for raw_message in rs:
				time.sleep(5)
				queue.put(raw_message)

	worker.terminate()

##############################################################################
# Process a newline-delimited list of URls
##############################################################################
def process_message(message, s3_output_bucket, s3_endpoint, job_id):
	try:
		output_dir = "/home/ec2-user/jobs/%s/" % (job_id)

		# Download images from URLs specified in message
		for line in message.splitlines():
			if line is None or line == "" or line == "\n" or not validate_uri(line):
				info_message("There was a junk url passed.")
				continue
			info_message("Downloading image from \"%s\"" % line)

			try:
				opt = "-P %s \"%s\"" % (output_dir, line)
				info_message("downloading from \"%s\"" % line)
				return_code = call("wget " + opt, shell=True)
				if return_code != 0:
					info_message("wget exited with %s" % return_code)
					continue
			except OSError as e:
				info_message("going to keep working but look at this error: %s" % e)
				continue

		output_image_name = "output-%s.jpg" % (job_id)
		output_image_path = output_dir + output_image_name 

		try:
			# Invoke ImageMagick to create a montage
			opts = " -size 400x400 null: %s*.* null: -thumbnail 400x400 -bordercolor white -background black +polaroid -resize 80%% -gravity center -background black -geometry -10+2  -tile x1 %s" % (output_dir, output_image_path)
			return_code = call("montage " + opts, shell=True)

			if return_code != 0:
				info_message("montage exited with %s" % return_code)
				return None
				# os.system("montage -size 400x400 null: %s*.* null: -thumbnail 400x400 -bordercolor white -background black +polaroid -resize 80%% -gravity center -background black -geometry -10+2  -tile x1 %s" % (output_dir, output_image_path))
		except Exception as e:
			info_message("Something went wrong with montage: %s" % e)
			return None

		# Write the resulting image to s3
		output_url = write_image_to_s3(output_image_path, output_image_name, s3_output_bucket, s3_endpoint)

		# Return the output url
		return output_url
	except:
		error_message("An error occurred. Please show this to your class instructor.")
		error_message(sys.exc_info()[0])
		return None
		
##############################################################################
# Write the result of a job to the output queue
##############################################################################		
def write_output_message(message, output_queue):
	m = RawMessage()
	m.set_body(message)
	status = output_queue.write(m)
	
##############################################################################
# Write an image to S3
##############################################################################
def write_image_to_s3(path, file_name, s3_output_bucket, s3_endpoint):
	# Connect to S3 and get the output bucket
	s3 = s3_connection(s3_endpoint, 0)
	output_bucket = s3.get_bucket(s3_output_bucket)

	# if os.path.exists(path):
	# Create a key to store the instances_json text
	k = Key(output_bucket)
	k.key = "out/" + file_name
	k.set_metadata("Content-Type", "image/jpeg")
	k.set_contents_from_filename(path)
	k.set_acl('public-read')

	# Return a URL to the object
	return "https://%s.s3.amazonaws.com/%s" % (s3_output_bucket, k.key)


def clean_up_job(job_id):
	try:
		output_dir = "/home/ec2-user/jobs/%s/" % (job_id)
		shutil.rmtree(output_dir)
	except:
		error_message("error deleting %s" % output_dir)
		pass


def s3_connection(s3_endpoint, retry):
	retry = 0
	try:
		return boto.connect_s3(host=s3_endpoint)
	except:
		if retry < RETRY_COUNT:
			retry += 1
			time.sleep(5)

			s3_connection(s3_endpoint, retry)

##############################################################################
# Verify S3 bucket, create it if required
##############################################################################
def create_s3_output_bucket(s3_output_bucket, s3_endpoint, region_name):
	# Connect to S3
	s3 = s3_connection(s3_endpoint, 0)

	# Find any existing buckets starting with 'image-bucket'
	buckets = [bucket.name for bucket in s3.get_all_buckets() if bucket.name.startswith('image-bucket')]
	if len(buckets) > 0:
	  return buckets[0]
	
	# No buckets, so create one for them
	name = 'image-bucket-' + str(uuid.uuid4())
	s3.create_bucket(name, location=region_name)
	return name


def validate_uri(uri, scheme=True):
    regex = re.compile(
        r'^(?:http|ftp)s?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'  # domain...
        r'localhost|'  # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', flags=re.IGNORECASE)

    return regex.match(uri)

##############################################################################
# Use logging class to log simple info messages
##############################################################################
def info_message(message):
	logger.info(message)

def error_message(message):
	logger.error(message)

##############################################################################
# Generic stirng logging
##############################################################################
class Logger:
	def __init__(self):
		#self.stream = StringIO.StringIO()
		#self.stream_handler = logging.StreamHandler(self.stream)
		self.file_handler = logging.FileHandler('/home/ec2-user/image_processor.log')
		self.log = logging.getLogger('image-processor')
		self.log.setLevel(logging.INFO)
		for handler in self.log.handlers: 
			self.log.removeHandler(handler)
		self.log.addHandler(self.file_handler)
		
	def info(self, message):
		try:
			self.log.info(time.asctime() + " " + message)
		except:
			self.log.info(message)
		
	def error(self, message):
		try:
			self.log.error(time.asctime() + " " + message)
		except:
			self.log.error(message)

logger = Logger()

if __name__ == "__main__":
    sys.exit(main())
