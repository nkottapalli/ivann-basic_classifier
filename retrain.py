from datetime import datetime
import hashlib
import os
import os.path
import random
import re
import sys
import tarfile

import numpy as np
from six.moves import urllib
import tensorflow as tf

from tensorflow.contrib.quantize.python import quant_ops
from tensorflow.python.framework import graph_util
from tensorflow.python.framework import tensor_shape
from tensorflow.python.platform import gfile
from tensorflow.python.util import compat


MIN_NUM_IMAGES_REQUIRED_FOR_TRAINING = 10
MIN_NUM_IMAGES_SUGGESTED_FOR_TRAINING = 100
MIN_NUM_IMAGES_REQUIRED_FOR_TESTING = 3
MAX_NUM_IMAGES_PER_CLASS = 2 ** 27 - 1
TRAINING_IMAGES_DIR = os.getcwd() + '/training_images'
TEST_IMAGES_DIR = os.getcwd() + "/test_images/"
OUTPUT_GRAPH = os.getcwd() + '/' + 'retrained_graph.pb'
INTERMEDIATE_OUTPUT_GRAPHS_DIR = os.getcwd() + '/intermediate_graph'
INTERMEDIATE_STORE_FREQUENCY = 0
OUTPUT_LABELS = os.getcwd() + '/' + 'retrained_labels.txt'
TENSORBOARD_DIR = os.getcwd() + '/' + 'tensorboard_logs'
HOW_MANY_TRAINING_STEPS=500
LEARNING_RATE = 0.01
TESTING_PERCENTAGE = 10
VALIDATION_PERCENTAGE = 10
EVAL_STEP_INTERVAL = 10
TRAIN_BATCH_SIZE = 100
TEST_BATCH_SIZE = -1
VALIDATION_BATCH_SIZE = 100
PRINT_MISCLASSIFIED_TEST_IMAGES = False
MODEL_DIR = os.getcwd() + "/" + "model"
BOTTLENECK_DIR = os.getcwd() + '/' + 'bottleneck_data'
FINAL_TENSOR_NAME = 'final_result'
FLIP_LEFT_RIGHT = False
RANDOM_CROP = 0
RANDOM_SCALE = 0
RANDOM_BRIGHTNESS = 0
ARCHITECTURE = 'inception_v3'

def main():
	print("starting program . . .")
	tf.logging.set_verbosity(tf.logging.INFO)
	if not checkIfNecessaryPathsAndFilesExist():
		return
	prepare_file_system()
	model_info = create_model_info(ARCHITECTURE)
	if not model_info:
		tf.logging.error('Did not recognize architecture flag')
		return -1
	print("downloading model (if necessary) . . .")
	downloadModelIfNotAlreadyPresent(model_info['data_url'])
	print("creating model graph . . .")
	graph, bottleneck_tensor, resized_image_tensor = (create_model_graph(model_info))
	print("creating image lists . . .")
	image_lists = create_image_lists(TRAINING_IMAGES_DIR, TESTING_PERCENTAGE, VALIDATION_PERCENTAGE)
	class_count = len(image_lists.keys())
	if class_count == 0:
		tf.logging.error('No valid folders of images found at ' + TRAINING_IMAGES_DIR)
		return -1
	if class_count == 1:
		tf.logging.error('Only one valid folder of images found at ' + TRAINING_IMAGES_DIR + ' - multiple classes are needed for classification.')
		return -1
	doDistortImages = False
	if (FLIP_LEFT_RIGHT == True or RANDOM_CROP != 0 or RANDOM_SCALE != 0 or RANDOM_BRIGHTNESS != 0):
		doDistortImages = True
	print("starting session . . .")
	with tf.Session(graph=graph) as sess:
		print("performing jpeg decoding . . .")
		jpeg_data_tensor, decoded_image_tensor = add_jpeg_decoding( model_info['input_width'], model_info['input_height'], model_info['input_depth'], model_info['input_mean'], model_info['input_std'])
		print("caching bottlenecks . . .")
		distorted_jpeg_data_tensor = None
		distorted_image_tensor = None
		if doDistortImages:
			(distorted_jpeg_data_tensor, distorted_image_tensor) = add_input_distortions(FLIP_LEFT_RIGHT, RANDOM_CROP, RANDOM_SCALE, RANDOM_BRIGHTNESS, model_info['input_width'], model_info['input_height'], model_info['input_depth'], model_info['input_mean'], model_info['input_std'])
		else:
			cache_bottlenecks(sess, image_lists, TRAINING_IMAGES_DIR, BOTTLENECK_DIR, jpeg_data_tensor, decoded_image_tensor, resized_image_tensor, bottleneck_tensor, ARCHITECTURE)
		print("adding final training layer . . .")
		(train_step, cross_entropy, bottleneck_input, ground_truth_input, final_tensor) = add_final_training_ops(len(image_lists.keys()), FINAL_TENSOR_NAME, bottleneck_tensor, model_info['bottleneck_tensor_size'], model_info['quantize_layer'])
		print("adding eval ops for final training layer . . .")
		evaluation_step, prediction = add_evaluation_step(final_tensor, ground_truth_input)
		print("writing TensorBoard info . . .")
		merged = tf.summary.merge_all()
		train_writer = tf.summary.FileWriter(TENSORBOARD_DIR + '/train', sess.graph)
		validation_writer = tf.summary.FileWriter(TENSORBOARD_DIR + '/validation')
		init = tf.global_variables_initializer()
		sess.run(init)
		print("performing training . . .")
		for i in range(HOW_MANY_TRAINING_STEPS):
			if doDistortImages:
				(train_bottlenecks, train_ground_truth) = get_random_distorted_bottlenecks(sess, image_lists, TRAIN_BATCH_SIZE, 'training', TRAINING_IMAGES_DIR, distorted_jpeg_data_tensor, distorted_image_tensor, resized_image_tensor, bottleneck_tensor)
			else:
				(train_bottlenecks, train_ground_truth, _) = get_random_cached_bottlenecks(sess, image_lists, TRAIN_BATCH_SIZE, 'training', BOTTLENECK_DIR, TRAINING_IMAGES_DIR, jpeg_data_tensor, decoded_image_tensor, resized_image_tensor, bottleneck_tensor, ARCHITECTURE)
			train_summary, _ = sess.run([merged, train_step], feed_dict={bottleneck_input: train_bottlenecks, ground_truth_input: train_ground_truth})
			train_writer.add_summary(train_summary, i)
			is_last_step = (i + 1 == HOW_MANY_TRAINING_STEPS)
			if (i % EVAL_STEP_INTERVAL) == 0 or is_last_step:
				train_accuracy, cross_entropy_value = sess.run([evaluation_step, cross_entropy], feed_dict={bottleneck_input: train_bottlenecks, ground_truth_input: train_ground_truth})
				tf.logging.info('%s: Step %d: Train accuracy = %.1f%%' % (datetime.now(), i, train_accuracy * 100))
				tf.logging.info('%s: Step %d: Cross entropy = %f' % (datetime.now(), i, cross_entropy_value))
				validation_bottlenecks, validation_ground_truth, _ = (get_random_cached_bottlenecks(sess, image_lists, VALIDATION_BATCH_SIZE, 'validation', BOTTLENECK_DIR, TRAINING_IMAGES_DIR, jpeg_data_tensor, decoded_image_tensor, resized_image_tensor, bottleneck_tensor, ARCHITECTURE))
				validation_summary, validation_accuracy = sess.run([merged, evaluation_step], feed_dict={bottleneck_input: validation_bottlenecks, ground_truth_input: validation_ground_truth})
				validation_writer.add_summary(validation_summary, i)
				tf.logging.info('%s: Step %d: Validation accuracy = %.1f%% (N=%d)' % (datetime.now(), i, validation_accuracy * 100, len(validation_bottlenecks)))
			intermediate_frequency = INTERMEDIATE_STORE_FREQUENCY
			if (intermediate_frequency > 0 and (i % intermediate_frequency == 0) and i > 0):
				intermediate_file_name = (INTERMEDIATE_OUTPUT_GRAPHS_DIR + 'intermediate_' + str(i) + '.pb')
				tf.logging.info('Save intermediate result to : ' + intermediate_file_name)
				save_graph_to_file(sess, graph, intermediate_file_name)
		print("running testing . . .")
		test_bottlenecks, test_ground_truth, test_filenames = (get_random_cached_bottlenecks(sess, image_lists, TEST_BATCH_SIZE, 'testing', BOTTLENECK_DIR, TRAINING_IMAGES_DIR, jpeg_data_tensor, decoded_image_tensor, resized_image_tensor, bottleneck_tensor, ARCHITECTURE))
		test_accuracy, predictions = sess.run([evaluation_step, prediction], feed_dict={bottleneck_input: test_bottlenecks, ground_truth_input: test_ground_truth})
		tf.logging.info('Final test accuracy = %.1f%% (N=%d)' % (test_accuracy * 100, len(test_bottlenecks)))
		if PRINT_MISCLASSIFIED_TEST_IMAGES:
			tf.logging.info('=== MISCLASSIFIED TEST IMAGES ===')
			for i, test_filename in enumerate(test_filenames):
				if predictions[i] != test_ground_truth[i]:
					tf.logging.info('%70s  %s' % (test_filename, list(image_lists.keys())[predictions[i]]))
		print("writing trained graph and labbels with weights")
		save_graph_to_file(sess, graph, OUTPUT_GRAPH)
		with gfile.FastGFile(OUTPUT_LABELS, 'w') as f:
			f.write('\n'.join(image_lists.keys()) + '\n')
		print("done !!")


def checkIfNecessaryPathsAndFilesExist():
	if not os.path.exists(TRAINING_IMAGES_DIR):
		print('')
		print('ERROR: TRAINING_IMAGES_DIR "' + TRAINING_IMAGES_DIR + '" does not seem to exist')
		print('Did you set up the training images?')
		print('')
		return False

	class TrainingSubDir:
		def __init__(self):
			self.loc = ""
			self.numImages = 0

	trainingSubDirs = []
	for dirName in os.listdir(TRAINING_IMAGES_DIR):
		currentTrainingImagesSubDir = os.path.join(TRAINING_IMAGES_DIR, dirName)
		if os.path.isdir(currentTrainingImagesSubDir):
			trainingSubDir = TrainingSubDir()
			trainingSubDir.loc = currentTrainingImagesSubDir
			trainingSubDirs.append(trainingSubDir)
	if len(trainingSubDirs) == 0:
		print("ERROR: there don't seem to be any training image sub-directories in " + TRAINING_IMAGES_DIR)
		print("Did you make a separare image sub-directory for each classification type?")
		return False
	for trainingSubDir in trainingSubDirs:
		for fileName in os.listdir(trainingSubDir.loc):
			if fileName.endswith(".jpg"):
				trainingSubDir.numImages += 1
	for trainingSubDir in trainingSubDirs:
		if trainingSubDir.numImages < MIN_NUM_IMAGES_REQUIRED_FOR_TRAINING:
			print("ERROR: there are less than the required " + str(MIN_NUM_IMAGES_REQUIRED_FOR_TRAINING) + " images in " + trainingSubDir.loc)
			print("Did you populate each training sub-directory with images?")
			return False
	for trainingSubDir in trainingSubDirs:
		if trainingSubDir.numImages < MIN_NUM_IMAGES_SUGGESTED_FOR_TRAINING:
			print("WARNING: there are less than the suggested " + str(MIN_NUM_IMAGES_SUGGESTED_FOR_TRAINING) + " images in " + trainingSubDir.loc)
			print("More images should be added to this directory for acceptable training results")
	if not os.path.exists(TEST_IMAGES_DIR):
		print('')
		print('ERROR: TEST_IMAGES_DIR "' + TEST_IMAGES_DIR + '" does not seem to exist')
		print('Did you break out some test images?')
		print('')
		return False
	numImagesInTestDir = 0
	for fileName in os.listdir(TEST_IMAGES_DIR):
		if fileName.endswith(".jpg"):
			numImagesInTestDir += 1
	if numImagesInTestDir < MIN_NUM_IMAGES_REQUIRED_FOR_TESTING:
		print("ERROR: there are not at least " + str(MIN_NUM_IMAGES_REQUIRED_FOR_TESTING) + " images in " + TEST_IMAGES_DIR)
		print("Did you break out some test images?")
		return False
	return True


def prepare_file_system():
	if tf.gfile.Exists(TENSORBOARD_DIR):
		tf.gfile.DeleteRecursively(TENSORBOARD_DIR)
	tf.gfile.MakeDirs(TENSORBOARD_DIR)
	if INTERMEDIATE_STORE_FREQUENCY > 0:
		makeDirIfDoesNotExist(INTERMEDIATE_OUTPUT_GRAPHS_DIR)
	return


def makeDirIfDoesNotExist(dir_name):
	if not os.path.exists(dir_name):
		os.makedirs(dir_name)


def create_model_info(architecture):
	architecture = architecture.lower()
	is_quantized = False
	if architecture == 'inception_v3':
		data_url = 'http://download.tensorflow.org/models/image/imagenet/inception-2015-12-05.tgz'
		bottleneck_tensor_name = 'pool_3/_reshape:0'
		bottleneck_tensor_size = 2048
		input_width = 299
		input_height = 299
		input_depth = 3
		resized_input_tensor_name = 'Mul:0'
		model_file_name = 'classify_image_graph_def.pb'
		input_mean = 128
		input_std = 128
	elif architecture.startswith('mobilenet_'):
		parts = architecture.split('_')
		if len(parts) != 3 and len(parts) != 4:
			tf.logging.error("Couldn't understand architecture name '%s'", architecture)
			return None
		version_string = parts[1]
		if (version_string != '1.0' and version_string != '0.75' and version_string != '0.50' and version_string != '0.25'):
			tf.logging.error(""""The Mobilenet version should be '1.0', '0.75', '0.50', or '0.25', but found '%s' for architecture '%s'""", version_string, architecture)
			return None
		size_string = parts[2]
		if (size_string != '224' and size_string != '192' and size_string != '160' and size_string != '128'):
			tf.logging.error("""The Mobilenet input size should be '224', '192', '160', or '128', but found '%s' for architecture '%s'""", size_string, architecture)
			return None
		if len(parts) == 3:
			is_quantized = False
		else:
			if parts[3] != 'quantized':
				tf.logging.error("Couldn't understand architecture suffix '%s' for '%s'", parts[3], architecture)
				return None
		is_quantized = True
		if is_quantized:
			data_url = 'http://download.tensorflow.org/models/mobilenet_v1_'
			data_url += version_string + '_' + size_string + '_quantized_frozen.tgz'
			bottleneck_tensor_name = 'MobilenetV1/Predictions/Reshape:0'
			resized_input_tensor_name = 'Placeholder:0'
			model_dir_name = ('mobilenet_v1_' + version_string + '_' + size_string + '_quantized_frozen')
			model_base_name = 'quantized_frozen_graph.pb'
		else:
			data_url = 'http://download.tensorflow.org/models/mobilenet_v1_'
			data_url += version_string + '_' + size_string + '_frozen.tgz'
			bottleneck_tensor_name = 'MobilenetV1/Predictions/Reshape:0'
			resized_input_tensor_name = 'input:0'
			model_dir_name = 'mobilenet_v1_' + version_string + '_' + size_string
			model_base_name = 'frozen_graph.pb'
		bottleneck_tensor_size = 1001
		input_width = int(size_string)
		input_height = int(size_string)
		input_depth = 3
		model_file_name = os.path.join(model_dir_name, model_base_name)
		input_mean = 127.5
		input_std = 127.5
	else:
		tf.logging.error("Couldn't understand architecture name '%s'", architecture)
		raise ValueError('Unknown architecture', architecture)
	return {'data_url': data_url, 'bottleneck_tensor_name': bottleneck_tensor_name, 'bottleneck_tensor_size': bottleneck_tensor_size, 'input_width': input_width, 'input_height': input_height, 'input_depth': input_depth, 'resized_input_tensor_name': resized_input_tensor_name, 'model_file_name': model_file_name, 'input_mean': input_mean, 'input_std': input_std, 'quantize_layer': is_quantized, }


def downloadModelIfNotAlreadyPresent(data_url):
	dest_directory = MODEL_DIR
	if not os.path.exists(dest_directory):
		os.makedirs(dest_directory)
	filename = data_url.split('/')[-1]
	filepath = os.path.join(dest_directory, filename)
	if not os.path.exists(filepath):
		def _progress(count, block_size, total_size):
			sys.stdout.write('\r>> Downloading %s %.1f%%' % (filename, float(count * block_size) / float(total_size) * 100.0))
		filepath, _ = urllib.request.urlretrieve(data_url, filepath, _progress)
		print()
		statinfo = os.stat(filepath)
		tf.logging.info('Successfully downloaded ' + str(filename) + ', statinfo.st_size = ' + str(statinfo.st_size) + ' bytes')
		print('Extracting file from ', filepath)
		tarfile.open(filepath, 'r:gz').extractall(dest_directory)
	else:
		print('Not extracting or downloading files, model already present in disk')


def create_model_graph(model_info):
	with tf.Graph().as_default() as graph:
		model_path = os.path.join(MODEL_DIR, model_info['model_file_name'])
		print('Model path: ', model_path)
		with gfile.FastGFile(model_path, 'rb') as f:
			graph_def = tf.GraphDef()
			graph_def.ParseFromString(f.read())
			bottleneck_tensor, resized_input_tensor = (tf.import_graph_def(graph_def, name='', return_elements=[model_info['bottleneck_tensor_name'], model_info['resized_input_tensor_name'],]))
	return graph, bottleneck_tensor, resized_input_tensor


def create_image_lists(image_dir, testing_percentage, validation_percentage):
	if not gfile.Exists(image_dir):
		tf.logging.error("Image directory '" + image_dir + "' not found.")
		return None
	result = {}
	sub_dirs = [x[0] for x in gfile.Walk(image_dir)]
	is_root_dir = True
	for sub_dir in sub_dirs:
		if is_root_dir:
			is_root_dir = False
			continue
		dir_name = os.path.basename(sub_dir)
		if dir_name == image_dir:
			continue
		# ToDo: This section should be refactored.  The right way to do this would be to get a list of the files that are
		# ToDo: there then append (extend) those, not to get the name except the extension, then append an extension,
		# ToDo: this (current) way is error prone of the original file has an upper case or mixed case extension
		extensions = ['jpg', 'jpeg']
		file_list = []
		tf.logging.info("Looking for images in '" + dir_name + "'")
		for extension in extensions:
			file_glob = os.path.join(image_dir, dir_name, '*.' + extension)
			file_list.extend(gfile.Glob(file_glob))
		if not file_list:
			tf.logging.warning('No files found')
			continue
		if len(file_list) < 20:
			tf.logging.warning('WARNING: Folder has less than 20 images, which may cause issues.')
		elif len(file_list) > MAX_NUM_IMAGES_PER_CLASS:
			tf.logging.warning('WARNING: Folder {} has more than {} images. Some images will never be selected.'.format(dir_name, MAX_NUM_IMAGES_PER_CLASS))
		label_name = re.sub(r'[^a-z0-9]+', ' ', dir_name.lower())
		training_images = []
		testing_images = []
		validation_images = []
		for file_name in file_list:
			base_name = os.path.basename(file_name)
			hash_name = re.sub(r'_nohash_.*$', '', file_name)
			hash_name_hashed = hashlib.sha1(compat.as_bytes(hash_name)).hexdigest()
			percentage_hash = ((int(hash_name_hashed, 16) % (MAX_NUM_IMAGES_PER_CLASS + 1)) * (100.0 / MAX_NUM_IMAGES_PER_CLASS))
			if percentage_hash < validation_percentage:
				validation_images.append(base_name)
			elif percentage_hash < (testing_percentage + validation_percentage):
				testing_images.append(base_name)
			else:
				training_images.append(base_name)
		result[label_name] = {'dir': dir_name, 'training': training_images, 'testing': testing_images, 'validation': validation_images,}
	return result



def add_jpeg_decoding(input_width, input_height, input_depth, input_mean, input_std):
	jpeg_data = tf.placeholder(tf.string, name='DecodeJPGInput')
	decoded_image = tf.image.decode_jpeg(jpeg_data, channels=input_depth)
	decoded_image_as_float = tf.cast(decoded_image, dtype=tf.float32)
	decoded_image_4d = tf.expand_dims(decoded_image_as_float, 0)
	resize_shape = tf.stack([input_height, input_width])
	resize_shape_as_int = tf.cast(resize_shape, dtype=tf.int32)
	resized_image = tf.image.resize_bilinear(decoded_image_4d, resize_shape_as_int)
	offset_image = tf.subtract(resized_image, input_mean)
	mul_image = tf.multiply(offset_image, 1.0 / input_std)
	return jpeg_data, mul_image



def add_input_distortions(flip_left_right, random_crop, random_scale, random_brightness, input_width, input_height, input_depth, input_mean, input_std):
	jpeg_data = tf.placeholder(tf.string, name='DistortJPGInput')
	decoded_image = tf.image.decode_jpeg(jpeg_data, channels=input_depth)
	decoded_image_as_float = tf.cast(decoded_image, dtype=tf.float32)
	decoded_image_4d = tf.expand_dims(decoded_image_as_float, 0)
	margin_scale = 1.0 + (random_crop / 100.0)
	resize_scale = 1.0 + (random_scale / 100.0)
	margin_scale_value = tf.constant(margin_scale)
	resize_scale_value = tf.random_uniform(tensor_shape.scalar(), minval=1.0, maxval=resize_scale)
	scale_value = tf.multiply(margin_scale_value, resize_scale_value)
	precrop_width = tf.multiply(scale_value, input_width)
	precrop_height = tf.multiply(scale_value, input_height)
	precrop_shape = tf.stack([precrop_height, precrop_width])
	precrop_shape_as_int = tf.cast(precrop_shape, dtype=tf.int32)
	precropped_image = tf.image.resize_bilinear(decoded_image_4d, precrop_shape_as_int)
	precropped_image_3d = tf.squeeze(precropped_image, squeeze_dims=[0])
	cropped_image = tf.random_crop(precropped_image_3d, [input_height, input_width, input_depth])
	if flip_left_right:
		flipped_image = tf.image.random_flip_left_right(cropped_image)
	else:
		flipped_image = cropped_image
	brightness_min = 1.0 - (random_brightness / 100.0)
	brightness_max = 1.0 + (random_brightness / 100.0)
	brightness_value = tf.random_uniform(tensor_shape.scalar(), minval=brightness_min, maxval=brightness_max)
	brightened_image = tf.multiply(flipped_image, brightness_value)
	offset_image = tf.subtract(brightened_image, input_mean)
	mul_image = tf.multiply(offset_image, 1.0 / input_std)
	distort_result = tf.expand_dims(mul_image, 0, name='DistortResult')
	return jpeg_data, distort_result


def cache_bottlenecks(sess, image_lists, image_dir, bottleneck_dir, jpeg_data_tensor, decoded_image_tensor, resized_input_tensor, bottleneck_tensor, architecture):
	how_many_bottlenecks = 0
	makeDirIfDoesNotExist(bottleneck_dir)
	for label_name, label_lists in image_lists.items():
		for category in ['training', 'testing', 'validation']:
			category_list = label_lists[category]
			for index, unused_base_name in enumerate(category_list):
				get_or_create_bottleneck(sess, image_lists, label_name, index, image_dir, category, bottleneck_dir, jpeg_data_tensor, decoded_image_tensor, resized_input_tensor, bottleneck_tensor, architecture)
			how_many_bottlenecks += 1
			if how_many_bottlenecks % 100 == 0:
				tf.logging.info(str(how_many_bottlenecks) + ' bottleneck files created.')


def get_or_create_bottleneck(sess, image_lists, label_name, index, image_dir, category, bottleneck_dir, jpeg_data_tensor, decoded_image_tensor, resized_input_tensor, bottleneck_tensor, architecture):
	label_lists = image_lists[label_name]
	sub_dir = label_lists['dir']
	sub_dir_path = os.path.join(bottleneck_dir, sub_dir)
	makeDirIfDoesNotExist(sub_dir_path)
	bottleneck_path = get_bottleneck_path(image_lists, label_name, index, bottleneck_dir, category, architecture)
	if not os.path.exists(bottleneck_path):
		create_bottleneck_file(bottleneck_path, image_lists, label_name, index, image_dir, category, sess, jpeg_data_tensor, decoded_image_tensor, resized_input_tensor, bottleneck_tensor)
	with open(bottleneck_path, 'r') as bottleneck_file:
		bottleneckBigString = bottleneck_file.read()
	bottleneckValues = []
	errorOccurred = False
	try:
		bottleneckValues = [float(individualString) for individualString in bottleneckBigString.split(',')]
	except ValueError:
		tf.logging.warning('Invalid float found, recreating bottleneck')
		errorOccurred = True
	if errorOccurred:
		create_bottleneck_file(bottleneck_path, image_lists, label_name, index, image_dir, category, sess, jpeg_data_tensor, decoded_image_tensor, resized_input_tensor, bottleneck_tensor)
		with open(bottleneck_path, 'r') as bottleneck_file:
			bottleneckBigString = bottleneck_file.read()
		bottleneckValues = [float(individualString) for individualString in bottleneckBigString.split(',')]
	return bottleneckValues


def get_bottleneck_path(image_lists, label_name, index, bottleneck_dir, category, architecture):
	return get_image_path(image_lists, label_name, index, bottleneck_dir, category) + '_' + architecture + '.txt'


def create_bottleneck_file(bottleneck_path, image_lists, label_name, index, image_dir, category, sess, jpeg_data_tensor, decoded_image_tensor, resized_input_tensor,bottleneck_tensor):
	tf.logging.info('Creating bottleneck at ' + bottleneck_path)
	image_path = get_image_path(image_lists, label_name, index, image_dir, category)
	if not gfile.Exists(image_path):
		tf.logging.fatal('File does not exist %s', image_path)
	image_data = gfile.FastGFile(image_path, 'rb').read()
	try:
		bottleneck_values = run_bottleneck_on_image(sess, image_data, jpeg_data_tensor, decoded_image_tensor, resized_input_tensor, bottleneck_tensor)
	except Exception as e:
		raise RuntimeError('Error during processing file %s (%s)' % (image_path, str(e)))
	bottleneck_string = ','.join(str(x) for x in bottleneck_values)
	with open(bottleneck_path, 'w') as bottleneck_file:
		bottleneck_file.write(bottleneck_string)


def run_bottleneck_on_image(sess, image_data, image_data_tensor, decoded_image_tensor, resized_input_tensor, bottleneck_tensor):
	resized_input_values = sess.run(decoded_image_tensor, {image_data_tensor: image_data})
	bottleneck_values = sess.run(bottleneck_tensor, {resized_input_tensor: resized_input_values})
	bottleneck_values = np.squeeze(bottleneck_values)
	return bottleneck_values


def get_image_path(image_lists, label_name, index, image_dir, category):
	if label_name not in image_lists:
		tf.logging.fatal('Label does not exist %s.', label_name)
	label_lists = image_lists[label_name]
	if category not in label_lists:
		tf.logging.fatal('Category does not exist %s.', category)
	category_list = label_lists[category]
	if not category_list:
		tf.logging.fatal('Label %s has no images in the category %s.', label_name, category)
	mod_index = index % len(category_list)
	base_name = category_list[mod_index]
	sub_dir = label_lists['dir']
	full_path = os.path.join(image_dir, sub_dir, base_name)
	return full_path


def add_final_training_ops(class_count, final_tensor_name, bottleneck_tensor, bottleneck_tensor_size, quantize_layer):
	with tf.name_scope('input'):
		bottleneck_input = tf.placeholder_with_default(bottleneck_tensor, shape=[None, bottleneck_tensor_size], name='BottleneckInputPlaceholder')
		ground_truth_input = tf.placeholder(tf.int64, [None], name='GroundTruthInput')
	layer_name = 'final_training_ops'
	with tf.name_scope(layer_name):
		quantized_layer_weights = None
		quantized_layer_biases = None
		with tf.name_scope('weights'):
			initial_value = tf.truncated_normal([bottleneck_tensor_size, class_count], stddev=0.001)
			layer_weights = tf.Variable(initial_value, name='final_weights')
			if quantize_layer:
				quantized_layer_weights = quant_ops.MovingAvgQuantize(layer_weights, is_training=True)
				attachTensorBoardSummaries(quantized_layer_weights)
				attachTensorBoardSummaries(layer_weights)
		with tf.name_scope('biases'):
			layer_biases = tf.Variable(tf.zeros([class_count]), name='final_biases')
			if quantize_layer:
				quantized_layer_biases = quant_ops.MovingAvgQuantize(layer_biases, is_training=True)
				attachTensorBoardSummaries(quantized_layer_biases)
			attachTensorBoardSummaries(layer_biases)
		with tf.name_scope('Wx_plus_b'):
			if quantize_layer:
				logits = tf.matmul(bottleneck_input, quantized_layer_weights) + quantized_layer_biases
				logits = quant_ops.MovingAvgQuantize(logits, init_min=-32.0, init_max=32.0, is_training=True, num_bits=8, narrow_range=False, ema_decay=0.5)
				tf.summary.histogram('pre_activations', logits)
			else:
				logits = tf.matmul(bottleneck_input, layer_weights) + layer_biases
				tf.summary.histogram('pre_activations', logits)
	final_tensor = tf.nn.softmax(logits, name=final_tensor_name)
	tf.summary.histogram('activations', final_tensor)
	with tf.name_scope('cross_entropy'):
		cross_entropy_mean = tf.losses.sparse_softmax_cross_entropy(labels=ground_truth_input, logits=logits)
	tf.summary.scalar('cross_entropy', cross_entropy_mean)
	with tf.name_scope('train'):
		optimizer = tf.train.GradientDescentOptimizer(LEARNING_RATE)
		train_step = optimizer.minimize(cross_entropy_mean)
	return (train_step, cross_entropy_mean, bottleneck_input, ground_truth_input, final_tensor)


def attachTensorBoardSummaries(var):
	with tf.name_scope('summaries'):
		mean = tf.reduce_mean(var)
		tf.summary.scalar('mean', mean)
		with tf.name_scope('stddev'):
			stddev = tf.sqrt(tf.reduce_mean(tf.square(var - mean)))
		tf.summary.scalar('stddev', stddev)
		tf.summary.scalar('max', tf.reduce_max(var))
		tf.summary.scalar('min', tf.reduce_min(var))
		tf.summary.histogram('histogram', var)


def add_evaluation_step(result_tensor, ground_truth_tensor):
	with tf.name_scope('accuracy'):
		with tf.name_scope('correct_prediction'):
			prediction = tf.argmax(result_tensor, 1)
			correct_prediction = tf.equal(prediction, ground_truth_tensor)
		with tf.name_scope('accuracy'):
			evaluation_step = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))
	tf.summary.scalar('accuracy', evaluation_step)
	return evaluation_step, prediction


def get_random_distorted_bottlenecks(sess, image_lists, how_many, category, image_dir, input_jpeg_tensor, distorted_image, resized_input_tensor, bottleneck_tensor):
	class_count = len(image_lists.keys())
	bottlenecks = []
	ground_truths = []
	for unused_i in range(how_many):
		label_index = random.randrange(class_count)
		label_name = list(image_lists.keys())[label_index]
		image_index = random.randrange(MAX_NUM_IMAGES_PER_CLASS + 1)
		image_path = get_image_path(image_lists, label_name, image_index, image_dir, category)
		if not gfile.Exists(image_path):
			tf.logging.fatal('File does not exist %s', image_path)
		jpeg_data = gfile.FastGFile(image_path, 'rb').read()
		distorted_image_data = sess.run(distorted_image, {input_jpeg_tensor: jpeg_data})
		bottleneck_values = sess.run(bottleneck_tensor, {resized_input_tensor: distorted_image_data})
		bottleneck_values = np.squeeze(bottleneck_values)
		bottlenecks.append(bottleneck_values)
		ground_truths.append(label_index)
	return bottlenecks, ground_truths


def get_random_cached_bottlenecks(sess, image_lists, how_many, category, bottleneck_dir, image_dir, jpeg_data_tensor, decoded_image_tensor, resized_input_tensor, bottleneck_tensor, architecture):
	class_count = len(image_lists.keys())
	bottlenecks = []
	ground_truths = []
	filenames = []
	if how_many >= 0:
		for unused_i in range(how_many):
			label_index = random.randrange(class_count)
			label_name = list(image_lists.keys())[label_index]
			image_index = random.randrange(MAX_NUM_IMAGES_PER_CLASS + 1)
			image_name = get_image_path(image_lists, label_name, image_index, image_dir, category)
			bottleneck = get_or_create_bottleneck(sess, image_lists, label_name, image_index, image_dir, category, bottleneck_dir, jpeg_data_tensor, decoded_image_tensor, resized_input_tensor, bottleneck_tensor, architecture)
			bottlenecks.append(bottleneck)
			ground_truths.append(label_index)
			filenames.append(image_name)
	else:
		for label_index, label_name in enumerate(image_lists.keys()):
			for image_index, image_name in enumerate(image_lists[label_name][category]):
				image_name = get_image_path(image_lists, label_name, image_index, image_dir, category)
				bottleneck = get_or_create_bottleneck(sess, image_lists, label_name, image_index, image_dir, category, bottleneck_dir, jpeg_data_tensor, decoded_image_tensor, resized_input_tensor, bottleneck_tensor, architecture)
				bottlenecks.append(bottleneck)
				ground_truths.append(label_index)
				filenames.append(image_name)
	return bottlenecks, ground_truths, filenames


def save_graph_to_file(sess, graph, graph_file_name):
	output_graph_def = graph_util.convert_variables_to_constants(sess, graph.as_graph_def(), [FINAL_TENSOR_NAME])
	with gfile.FastGFile(graph_file_name, 'wb') as f:
		f.write(output_graph_def.SerializeToString())
	return


if __name__ == '__main__':
	main()
