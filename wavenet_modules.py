import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable, Function

import numpy as np

class ConvDilated(nn.Module):
	def __init__(self,
				 num_channels_in=1,
				 num_channels_out=1,
				 kernel_size=2,
				 dilation=2,
				 residual_connection=True):
		super(ConvDilated, self).__init__()

		self.num_channels_in = num_channels_in
		self.num_channels_out = num_channels_out
		self.kernel_size = kernel_size

		self.conv = nn.Conv1d(in_channels=num_channels_in,
							  out_channels=num_channels_out,
							  kernel_size=kernel_size,
							  bias=False)
		#self.batchnorm = nn.BatchNorm1d(num_features=num_channels_out, affine=False)

		self.dilation = dilation
		self.residual = residual_connection

		self.queue = DilatedQueue(max_length=(kernel_size-1) * dilation + 1,
								  num_channels=num_channels_in,
								  dilation=dilation)

	def forward(self, x):
		x = dilate(x, self.dilation)

		l = x.size(2)
		# zero padding for convolution
		if l < self.kernel_size:
			x = constant_pad_1d(x, self.kernel_size-l, dimension=2, pad_start=True)

		r = self.conv(x)

		if self.residual:
			r = r + x[:,:,1:]
		# x = self.batchnorm()
		r = F.relu(r)
		return r

	def generate(self, new_sample):
		self.queue.enqueue(new_sample)
		x = self.queue.dequeue(num_deq=self.kernel_size,
							   dilation=self.dilation)

		x = x.unsqueeze(0)

		r = self.conv(Variable(x, volatile=True))

		if self.residual:
			r = r + x
		#x = self.batchnorm(x)
		r = F.relu(r)
		r = r.data.squeeze(0)
		return r

class Final(nn.Module):
	def __init__(self,
				 in_channels=1,
				 num_classes=256,
				 out_length=1024):
		super(Final, self).__init__()
		self.num_classes = num_classes
		self.in_channels = in_channels
		self.out_length = out_length
		self.conv = nn.Conv1d(in_channels,
							  num_classes,
							  kernel_size=1,
							  bias=True)
		#self.batchnorm = nn.BatchNorm1d(num_classes, affine=False)
		#nn.init.normal(self.conv.weight)
		#self.conv.weight = nn.Parameter(torch.FloatTensor([1]))

	def forward(self, x):
		#x = self.batchnorm(self.conv(x))
		x = dilate(x, 1)
		x = self.conv(x)

		[n, c, l] = x.size()
		x = x.transpose(1, 2).contiguous().view(n*l, c)
		x = x[-self.out_length:, :]
		return x

	def generate(self, x):
		#x = self.batchnorm(self.conv(Variable(x.unsqueeze(0), volatile=True)))
		x = x.unsqueeze(0)
		x = self.conv(Variable(x, volatile=True))
		x = x.squeeze()
		return x

def dilate(x, dilation):
	[n, c, l] = x.size()
	dilation_factor = dilation / n
	if dilation == n:
		return x

	# zero padding for reshaping
	new_l = int(np.ceil(l / dilation_factor) * dilation_factor)
	if new_l != l:
		l = new_l
		x = constant_pad_1d(x, new_l, dimension=2, pad_start=True)

	l = (l * n) // dilation
	n = dilation

	# reshape according to dilation
	x = x.permute(1, 2, 0).contiguous()
	x = x.view(c, l, n)
	x = x.permute(2, 0, 1)

	return x

class DilatedQueue:
	def __init__(self, max_length, data=None, dilation=1, num_deq=1, num_channels=1):
		self.in_pos = 0
		self.out_pos = 0
		self.num_deq = num_deq
		self.num_channels = num_channels
		self.dilation = dilation
		self.max_length = max_length
		self.data = data
		if data == None:
			self.data = torch.zeros(num_channels, max_length)

	def enqueue(self, input):
		self.data[:, self.in_pos] = input
		self.in_pos = (self.in_pos + 1) % self.max_length

	def dequeue(self, num_deq=1, dilation=1):
		#       |
		#  |6|7|8|1|2|3|4|5|
		#         |
		start = self.out_pos - ((num_deq - 1) * dilation)
		if start < 0:
			t1 = self.data[:, start::dilation]
			t2 = self.data[:, self.out_pos % dilation:self.out_pos+1:dilation]
			t = torch.cat((t1, t2), 1)
		else:
			t = self.data[:, start:self.out_pos+1:dilation]

		self.out_pos = (self.out_pos + 1) % self.max_length
		return t

	def reset(self):
		self.data = torch.torch.zeros(self.num_channels, self.max_length)
		self.in_pos = 0
		self.out_pos = 0

class ConstantPad1d(Function):
	def __init__(self, target_size, dimension=0, value=0, pad_start=False):
		super(ConstantPad1d, self).__init__()
		self.target_size = target_size
		self.dimension = dimension
		self.value = value
		self.pad_start = pad_start

	def forward(self, input):
		self.num_pad = self.target_size - input.size(self.dimension)
		assert self.num_pad >= 0, 'target size has to be greater than input size'

		self.input_size = input.size()

		size = list(input.size())
		size[self.dimension] = self.target_size
		output = input.new(*tuple(size)).fill_(self.value)
		c_output = output

		# crop output
		if self.pad_start:
			c_output = c_output.narrow(self.dimension, self.num_pad, c_output.size(self.dimension) - self.num_pad)
		else:
			c_output = c_output.narrow(self.dimension, 0, c_output.size(self.dimension) - self.num_pad)

		c_output.copy_(input)
		return output

	def backward(self, grad_output):
		grad_input = grad_output.new(*self.input_size).zero_()
		cg_output = grad_output

		# crop grad_output
		if self.pad_start:
			cg_output = cg_output.narrow(self.dimension, self.num_pad, cg_output.size(self.dimension) - self.num_pad)
		else:
			cg_output = cg_output.narrow(self.dimension, 0, cg_output.size(self.dimension) - self.num_pad)

		grad_input.copy_(cg_output)
		return grad_input


def constant_pad_1d(input,
					target_size,
					dimension=0,
					value=0,
					pad_start=False):
	return ConstantPad1d(target_size, dimension, value, pad_start)(input)