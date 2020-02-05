# Copyright 2020 The DDSP Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Library of base Processor and ProcessorGroup.

ProcessorGroup exists as an alternative to manually specifying the forward
propagation in python. The advantage is that a variety of configurations can be
programmatically specified via external dependency injection, such as with the
`gin` library.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from typing import Dict, Sequence, Tuple, Text

from absl import logging
from ddsp import core
import gin
import tensorflow.compat.v2 as tf

tfkl = tf.keras.layers

# Define Types.
TensorDict = Dict[Text, tf.Tensor]


# Processor Base Class ---------------------------------------------------------
class Processor(tfkl.Layer):
  """Abstract base class for signal processors.

  Since most effects / synths require specificly formatted control signals
  (such as amplitudes and frequenices), each processor implements a
  get_controls(inputs) method, where inputs are a variable number of tensor
  arguments that are typically neural network outputs. Check each child class
  for the class-specific arguments it expects. This gives a dictionary of
  controls that can then be passed to get_signal(controls). The
  get_outputs(inputs) method calls both in succession and returns a nested
  output dictionary with all controls and signals.
  """

  def __init__(self, name: Text, trainable: bool = False):
    super(Processor, self).__init__(name=name,
                                    trainable=trainable,
                                    autocast=False)

  def call(self, *args: tf.Tensor, **kwargs: tf.Tensor) -> tf.Tensor:
    """Convert input tensors arguments into a signal tensor."""
    controls = self.get_controls(*args, **kwargs)
    signal = self.get_signal(**controls)
    return signal

  def get_controls(self, *args: tf.Tensor, **kwargs: tf.Tensor) -> TensorDict:
    """Convert input tensor arguments into a dict of processor controls."""
    raise NotImplementedError

  def get_signal(self, *args: tf.Tensor, **kwargs: tf.Tensor) -> tf.Tensor:
    """Convert control tensors into a signal tensor."""
    raise NotImplementedError


# ProcessorGroup Class ---------------------------------------------------------
# Define Types.
Node = Tuple[Processor, Sequence[Text]]
DAG = Sequence[Node]


@gin.configurable
class ProcessorGroup(tfkl.Layer):
  """String Proccesor() objects together into a processor_group."""

  def __init__(self, dag: DAG, name: Text = 'processor_group'):
    """Constructor.

    Args:
      dag: A directed acyclical graph in the form of an iterable of tuples or
        dictionaries. Tuples are intepreted as (processor, [inputs]).
        "Processor" should be an instance of a Processor() object. "Inputs" is
        an iterable of strings each of which is a nested dict key. For example,
        "synth_additive/controls/f0_hz" would correspond to the value
        {"synth_additive": {"controls": {"f0_hz": value}}}.  The graph is read
        sequentially and must be topologically sorted. This means that all
        inputs for a processor must already be generated by earlier processors
        (or inputs to the processor_group).
      name: Name of processor_group.
    """
    super(ProcessorGroup, self).__init__(name=name)
    self.dag = dag
    # Collect a list of processors.
    self.processors = [node[0] for node in self.dag]

  def call(self, dag_inputs: TensorDict) -> tf.Tensor:
    """Like Processor, but specific to having an input dictionary."""
    dag_outputs = self.get_controls(dag_inputs)
    signal = self.get_signal(dag_outputs)
    return signal

  def get_controls(self, dag_inputs: TensorDict) -> TensorDict:
    """Run the DAG and get complete outputs dictionary for the processor_group.

    Args:
      dag_inputs: A dictionary of input tensors fed to the signal processing
        processor_group.

    Returns:
      A nested dictionary of all the output tensors.
    """
    # Initialize the outputs with inputs to the processor_group.
    outputs = dag_inputs

    # Run through the DAG nodes in sequential order.
    for node in self.dag:
      # Get the node processor and keys to the node input.
      processor, keys = node

      # Logging, only on the first call.
      if not self.built:
        logging.info('Connecting node (%s):', processor.name)
        for i, key in enumerate(keys):
          logging.info('Input %d: %s', i, key)

      # Get the inputs to the node.
      inputs = [core.nested_lookup(key, outputs) for key in keys]

      # Build the processor only if called the first time in a @tf.function.
      # Need to explicitly build because we use get_controls() and get_signal()
      # seperately, (to get intermediates) rather than directly using call().
      if not processor.built:
        processor.build([tensor.shape for tensor in inputs])

      # Run processor.
      controls = processor.get_controls(*inputs)
      signal = processor.get_signal(**controls)

      #  Add outputs to the dictionary.
      outputs[processor.name] = {'controls': controls, 'signal': signal}

    # Get output signal from last processor.
    output_name = self.processors[-1].name
    outputs[self.name] = {'signal': outputs[output_name]['signal']}

    # Logging, only on the first call.
    if not self.built:
      logging.info('ProcessorGroup output node (%s)', output_name)

    return outputs

  def get_signal(self, dag_outputs: TensorDict) -> tf.Tensor:
    """Extract the output signal from the dag outputs.

    Args:
      dag_outputs: A dictionary of tensors output from self.get_controls().

    Returns:
      Signal tensor.
    """
    # Initialize the outputs with inputs to the processor_group.
    return dag_outputs[self.name]['signal']


# Routing processors for manipulating signals in a processor_group -------------
@gin.register
class Add(Processor):
  """Sum two signals."""

  def __init__(self, name: Text = 'add'):
    super(Add, self).__init__(name=name)

  def get_controls(self, signal_one: tf.Tensor,
                   signal_two: tf.Tensor) -> TensorDict:
    """Just pass signals through."""
    return {'signal_one': signal_one, 'signal_two': signal_two}

  def get_signal(self, signal_one: tf.Tensor,
                 signal_two: tf.Tensor) -> tf.Tensor:
    return signal_one + signal_two


@gin.register
class Mix(Processor):
  """Constant-power crossfade between two signals."""

  def __init__(self, name: Text = 'mix'):
    super(Mix, self).__init__(name=name)

  def get_controls(self, signal_one: tf.Tensor, signal_two: tf.Tensor,
                   nn_out_mix_level: tf.Tensor) -> TensorDict:
    """Standardize inputs to same length, mix_level to range [0, 1].

    Args:
      signal_one: 2-D or 3-D tensor.
      signal_two: 2-D or 3-D tensor.
      nn_out_mix_level: Tensor of shape [batch, n_time, 1] output of the network
        determining relative levels of signal one and two.

    Returns:
      Dict of control parameters.

    Raises:
      ValueError: If signal_one and signal_two are not the same length.
    """
    n_time_one = int(signal_one.shape[1])
    n_time_two = int(signal_two.shape[1])
    if n_time_one != n_time_two:
      raise ValueError('The two signals must have the same length instead of'
                       '{} and {}'.format(n_time_one, n_time_two))

    mix_level = tf.nn.sigmoid(nn_out_mix_level)
    mix_level = core.resample(mix_level, n_time_one)
    return {
        'signal_one': signal_one,
        'signal_two': signal_two,
        'mix_level': mix_level
    }

  def get_signal(self, signal_one: tf.Tensor, signal_two: tf.Tensor,
                 mix_level: tf.Tensor) -> tf.Tensor:
    """Constant-power cross fade between two signals.

    Args:
      signal_one: 2-D or 3-D tensor.
      signal_two: 2-D or 3-D tensor.
      mix_level: Tensor of shape [batch, n_time, 1] determining relative levels
        of signal one and two. Must have same number of time steps as the other
        signals and be in the range [0, 1].

    Returns:
      Tensor of mixed output signal.
    """
    mix_level_one = tf.sqrt(tf.abs(mix_level))
    mix_level_two = 1.0 - tf.sqrt(tf.abs(mix_level - 1.0))
    return mix_level_one * signal_one + mix_level_two * signal_two
