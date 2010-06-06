"""Implementation of a thread-pool working with channels"""
from thread import WorkerThread
from Queue import Queue

from graph import (
		Graph, 
		Node
	)

from channel import (
		Channel,
		WChannel, 
		RChannel
	)

import weakref
import sys

class TaskNode(Node):
	"""Couples an input channel, an output channel, as well as a processing function
	together.
	It may contain additional information on how to handel read-errors from the
	input channel"""
	__slots__ = (	'in_rc',			# input read channel 
					'_out_wc', 			# output write channel
					'_pool_ref', 		# ref to our pool
					'_exc',				# exception caught
					'fun',				# function to call with items read from in_rc
					'min_count', 		# minimum amount of items to produce, None means no override
					'max_chunksize',	# maximium amount of items to process per process call
					'apply_single'		# apply single items even if multiple where read
					)
	
	def __init__(self, in_rc, fun, apply_single=True):
		self.in_rc = in_rc
		self._out_wc = None
		self._pool_ref = None
		self._exc = None
		self.fun = fun
		self.min_count = None
		self.max_chunksize = 0				# note set
		self.apply_single = apply_single
	
	def is_done(self):
		""":return: True if we are finished processing"""
		return self._out_wc.closed
		
	def set_done(self):
		"""Set ourselves to being done, has we have completed the processing"""
		self._out_wc.close()
		
	def error(self):
		""":return: Exception caught during last processing or None"""
		return self._exc

	def process(self, count=1):
		"""Process count items and send the result individually to the output channel"""
		if self._out_wc is None:
			raise IOError("Cannot work in uninitialized task")
		
		read = self.in_rc.read
		if isinstance(self.in_rc, RPoolChannel) and self.in_rc._pool is self._pool_ref():
			read = self.in_rc._read
		items = read(count)
		
		try:
			if self.apply_single:
				for item in items:
					self._out_wc.write(self.fun(item))
				# END for each item
			else:
				self._out_wc.write(self.fun(items))
			# END handle single apply
		except Exception, e:
			self._exc = e
			self.set_done()
		# END exception handling
		
		# if we didn't get all demanded items, which is also the case if count is 0
		# we have depleted the input channel and are done
		if len(items) != count:
			self.set_done()
		# END handle done state
	#{ Configuration
	

class RPoolChannel(RChannel):
	""" A read-only pool channel may not be wrapped or derived from, but it provides slots to call
	before and after an item is to be read.
	
	It acts like a handle to the underlying task in the pool."""
	__slots__ = ('_task', '_pool', '_pre_cb', '_post_cb')
	
	def __init__(self, wchannel, task, pool):
		RChannel.__init__(self, wchannel)
		self._task = task
		self._pool = pool
		self._pre_cb = None
		self._post_cb = None
		
	def __del__(self):
		"""Assures that our task will be deleted if we were the last reader"""
		del(self._wc)		# decrement ref-count
		self._pool._del_task_if_orphaned(self._task)
	
	def set_pre_cb(self, fun = lambda count: None):
		"""Install a callback to call with the item count to be read before any 
		item is actually  read from the channel.
		If it fails, the read will fail with an IOError
		If a function is not provided, the call is effectively uninstalled."""
		self._pre_cb = fun
	
	def set_post_cb(self, fun = lambda item: item):
		"""Install a callback to call after the items were read. The function
		returns a possibly changed item list. If it raises, the exception will be propagated.
		If a function is not provided, the call is effectively uninstalled."""
		self._post_cb = fun
		
	def read(self, count=1, block=False, timeout=None):
		"""Read an item that was processed by one of our threads
		:note: Triggers task dependency handling needed to provide the necessary 
			input"""
		if self._pre_cb:
			self._pre_cb()
		# END pre callback
		
		##################################################
		self._pool._prepare_processing(self._task, count)
		##################################################
		
		items = RChannel.read(self, count, block, timeout)
		if self._post_cb:
			items = self._post_cb(items)
		
	#{ Internal
	def _read(self, count=1, block=False, timeout=None):
		"""Calls the underlying channel's read directly, without triggering 
		the pool"""
		return RChannel.read(self, count, block, timeout)
	
	#} END internal
	
	
	
class ThreadPool(object):
	"""A thread pool maintains a set of one or more worker threads, but supports 
	a fully serial mode in which case the amount of threads is zero.
	
	Work is distributed via Channels, which form a dependency graph. The evaluation
	is lazy, as work will only be done once an output is requested.
	
	:note: the current implementation returns channels which are meant to be 
		used only from the main thread, hence you cannot consume their results 
		from multiple threads unless you use a task for it."""
	__slots__ = (	'_tasks',				# a graph of tasks
					'_consumed_tasks',		# a list with tasks that are done or had an error
					'_workers',				# list of worker threads
					'_queue', 				# master queue for tasks
					'_ordered_tasks_cache' # tasks in order of evaluation, mapped from task -> task list
				)
	
	def __init__(self, size=0):
		self._tasks = Graph()
		self._consumed_tasks = list()
		self._workers = list()
		self._queue = Queue()
		self._ordered_tasks_cache = dict()
		
	def __del__(self):
		raise NotImplementedError("TODO: Proper cleanup")
	
	#{ Internal
	def _queue_feeder_visitor(self, task, count):
		"""Walk the graph and find tasks that are done for later cleanup, and 
		queue all others for processing by our worker threads ( if available )."""
		if task.error() or task.is_done():
			self._consumed_tasks.append(task)
		
		# allow min-count override. This makes sure we take at least min-count
		# items off the input queue ( later )
		if task.min_count is not None:
			count = task.min_count
		# END handle min-count
		
		# if the task does not have the required output on its queue, schedule
		# it for processing. If we should process all, we don't care about the 
		# amount as it should process until its all done.
		if self._workers:
			if count < 1 or task._out_wc.size() < count:
				# respect the chunk size, and split the task up if we want 
				# to process too much. This can be defined per task
				queue = self._queue
				if task.max_chunksize:
					chunksize = count / task.max_chunksize
					remainder = count - (chunksize * task.max_chunksize)
					for i in xrange(chunksize):
						queue.put((task.process, chunksize))
					if remainder:
						queue.put((task.process, remainder))
				else:
					self._queue.put((task.process, count))
				# END handle chunksize
			# END handle queuing
		else:
			# no workers, so we have to do the work ourselves
			task.process(count)
		# END handle serial mode 
		
		# always walk the whole graph, we want to find consumed tasks
		return True
		
	def _prepare_processing(self, task, count):
		"""Process the tasks which depend on the given one to be sure the input 
		channels are filled with data once we process the actual task
		
		Tasks have two important states: either they are done, or they are done 
		and have an error, so they are likely not to have finished all their work.
		
		Either way, we will put them onto a list of tasks to delete them, providng 
		information about the failed ones.
		
		Tasks which are not done will be put onto the queue for processing, which 
		is fine as we walked them depth-first."""
		self._tasks.visit_input_inclusive_depth_first(task, lambda n: self._queue_feeder_visitor(n, count))
		
		# delete consumed tasks to cleanup
		for task in self._consumed_tasks:
			self.del_task(task)
		# END for each task to delete
		del(self._consumed_tasks[:])
		
	def _del_task_if_orphaned(self, task):
		"""Check the task, and delete it if it is orphaned"""
		if sys.getrefcount(task._out_wc) < 3:
			self.del_task(task)
	#} END internal
	
	#{ Interface 
	
	def del_task(self, task):
		"""Delete the task
		Additionally we will remove orphaned tasks, which can be identified if their 
		output channel is only held by themselves, so no one will ever consume 
		its items.
		
		:return: self"""
		# now delete our actual node - must set it done os it closes its channels.
		# Otherwise further reads of output tasks will block.
		# Actually they may still block if anyone wants to read all ... without 
		# a timeout
		# keep its input nodes as we check whether they were orphaned
		in_tasks = task.in_nodes
		task.set_done()
		self._tasks.del_node(task)
		
		for t in in_tasks
			self._del_task_if_orphaned(t)
		# END handle orphans recursively
		
		return self
	
	def set_pool_size(self, size=0):
		"""Set the amount of workers to use in this pool. When reducing the size, 
		the call may block as it waits for threads to finish. 
		When reducing the size to zero, this thread will process all remaining 
		items on the queue.
		
		:return: self
		:param size: if 0, the pool will do all work itself in the calling thread, 
			otherwise the work will be distributed among the given amount of threads"""
		# either start new threads, or kill existing ones.
		# If we end up with no threads, we process the remaining chunks on the queue
		# ourselves
		cur_count = len(self._workers)
		if cur_count < size:
			for i in range(size - cur_count):
				worker = WorkerThread(self._queue)
				self._workers.append(worker)
			# END for each new worker to create
		elif cur_count > size:
			del_count = cur_count - size
			for i in range(del_count):
				self._workers[i].stop_and_join()
			# END for each thread to stop
			del(self._workers[:del_count])
		# END handle count
		
		if size == 0:
			while not self._queue.empty():
				try:
					taskmethod, count = self._queue.get(False)
					taskmethod(count)
				except Queue.Empty:
					continue
			# END while there are tasks on the queue
		# END process queue
		return self
			
	def add_task(self, task):
		"""Add a new task to be processed.
		:return: a read channel to retrieve processed items. If that handle is lost, 
			the task will be considered orphaned and will be deleted on the next 
			occasion."""
		# create a write channel for it
		wc, rc = Channel()
		rc = RPoolChannel(wc, task, self)
		task._out_wc = wc
		task._pool_ref = weakref.ref(self)
		
		self._tasks.add_node(task)
		
		# If the input channel is one of our read channels, we add the relation
		ic = task.in_rc
		if isinstance(ic, RPoolChannel) and ic._pool is self:
			self._tasks.add_edge(ic._task, task)
		# END add task relation
		
		return rc
			
	#} END interface 
