from __future__ import division, print_function, absolute_import
from collections import defaultdict, OrderedDict, Counter
import numpy as np
import itertools
import time
import sys
import h5py
import json
from . import seqlets_to_patterns
from .. import core
from .. import coordproducers
from .. import metaclusterers
from .. import util


class TfModiscoResults(object):

    def __init__(self,
                 task_names,
                 multitask_seqlet_creation_results,
                 seqlet_attribute_vectors,
                 metaclustering_results,
                 metacluster_idx_to_submetacluster_results,
                 **kwargs):
        self.task_names = task_names
        self.multitask_seqlet_creation_results =\
                multitask_seqlet_creation_results
        self.seqlet_attribute_vectors = seqlet_attribute_vectors
        self.metaclustering_results = metaclustering_results
        self.metacluster_idx_to_submetacluster_results =\
            metacluster_idx_to_submetacluster_results

        self.__dict__.update(**kwargs)

    def save_hdf5(self, grp):
        util.save_string_list(string_list=self.task_names, 
                              dset_name="task_names", grp=grp)
        self.multitask_seqlet_creation_results.save_hdf5(
            grp.create_group("multitask_seqlet_creation_results"))
        self.metaclustering_results.save_hdf5(
            grp.create_group("metaclustering_results"))

        metacluster_idx_to_submetacluster_results_group = grp.create_group(
                                "metacluster_idx_to_submetacluster_results")
        for idx in self.metacluster_idx_to_submetacluster_results:
            self.metacluster_idx_to_submetacluster_results[idx].save_hdf5(
                grp=metacluster_idx_to_submetacluster_results_group
                    .create_group("metacluster"+str(idx))) 


class SubMetaclusterResults(object):

    def __init__(self, metacluster_size, activity_pattern,
                       seqlets, seqlets_to_patterns_result):
        self.metacluster_size = metacluster_size
        self.activity_pattern = activity_pattern
        self.seqlets = seqlets
        self.seqlets_to_patterns_result = seqlets_to_patterns_result

    def save_hdf5(self, grp):
        grp.attrs['size'] = self.metacluster_size
        grp.create_dataset('activity_pattern', data=self.activity_pattern)
        util.save_seqlet_coords(seqlets=self.seqlets,
                                dset_name="seqlets", grp=grp)   
        self.seqlets_to_patterns_result.save_hdf5(
            grp=grp.create_group('seqlets_to_patterns_result'))


class TfModiscoWorkflow(object):

    def __init__(self,
                 seqlets_to_patterns_factory=
                 seqlets_to_patterns.TfModiscoSeqletsToPatternsFactory(),
                 sliding_window_size=21, flank_size=10,
                 histogram_bins=100, percentiles_in_bandwidth=10, 
                 overlap_portion=0.5,
                 min_cluster_size=100,
                 laplace_threshold_cdf = "auto",
                 weak_threshold_for_counting_sign = 0.99,
                 verbose=True):

        self.seqlets_to_patterns_factory = seqlets_to_patterns_factory
        self.sliding_window_size = sliding_window_size
        self.flank_size = flank_size
        self.histogram_bins = histogram_bins
        self.percentiles_in_bandwidth = percentiles_in_bandwidth
        self.overlap_portion = overlap_portion
        self.min_cluster_size = min_cluster_size
        self.laplace_threshold_cdf = laplace_threshold_cdf
        self.weak_threshold_for_counting_sign =\
            weak_threshold_for_counting_sign
        self.verbose = verbose

        self.build()

    def build(self):
        
        self.overlap_resolver = core.SeqletsOverlapResolver(
            overlap_detector=core.CoordOverlapDetector(self.overlap_portion),
            seqlet_comparator=core.SeqletComparator(
                                value_provider=lambda x: x.coor.score))

        self.threshold_score_transformer_factory =\
            core.LaplaceCdfFactory(flank_to_ignore=self.flank_size)

    def __call__(self, task_names, contrib_scores,
                       hypothetical_contribs, one_hot):

        if (self.laplace_threshold_cdf == "auto"):
            total_num = sum([(1+len(x)-self.sliding_window_size)
                             for x in one_hot])
            assert total_num > self.min_cluster_size, (
                    "Increase min_cluster_size "+str(self.min_cluster_size))
            laplace_threshold_cdf = max(
                    min(0.99, 1-(self.min_cluster_size/total_num)),
                    1-(500/total_num))
        else:
            laplace_threshold_cdf = self.laplace_threshold_cdf
        print("Using laplace threshold of "+str(laplace_threshold_cdf))

        if (self.weak_threshold_for_counting_sign is None):
            weak_threshold_for_counting_sign = laplace_threshold_cdf
        else:
            weak_threshold_for_counting_sign =\
                self.weak_threshold_for_counting_sign
        if (weak_threshold_for_counting_sign > laplace_threshold_cdf):
            print("Reducing weak_threshold_for_counting_sign to"
                  +" match laplace_threshold_cdf, from "
                  +str(weak_threshold_for_counting_sign)
                  +" to "+str(laplace_threshold_cdf))
            weak_threshold_for_counting_sign = laplace_threshold_cdf

        self.coord_producer = coordproducers.FixedWindowAroundChunks(
            sliding=self.sliding_window_size,
            flank=self.flank_size,
            thresholding_function=coordproducers.LaplaceThreshold(
                                    threshold_cdf=laplace_threshold_cdf,
                                    verbose=self.verbose),
            verbose=self.verbose) 

        self.metaclusterer = metaclusterers.SignBasedPatternClustering(
                                min_cluster_size=self.min_cluster_size,
                                threshold_for_counting_sign=
                                    laplace_threshold_cdf,
                                weak_threshold_for_counting_sign=
                                    weak_threshold_for_counting_sign)

        contrib_scores_tracks = [
            core.DataTrack(
                name=key+"_contrib_scores",
                fwd_tracks=contrib_scores[key],
                rev_tracks=[x[::-1, ::-1] for x in 
                            contrib_scores[key]],
                has_pos_axis=True) for key in task_names] 

        hypothetical_contribs_tracks = [
            core.DataTrack(name=key+"_hypothetical_contribs",
                           fwd_tracks=hypothetical_contribs[key],
                           rev_tracks=[x[::-1, ::-1] for x in 
                                        hypothetical_contribs[key]],
                           has_pos_axis=True)
                           for key in task_names]

        onehot_track = core.DataTrack(
                            name="sequence", fwd_tracks=one_hot,
                            rev_tracks=[x[::-1, ::-1] for x in one_hot],
                            has_pos_axis=True)

        track_set = core.TrackSet(
                        data_tracks=contrib_scores_tracks
                        +hypothetical_contribs_tracks+[onehot_track])

        per_position_contrib_scores = OrderedDict([
            (x, np.sum(contrib_scores[x],axis=2)) for x in task_names])

        task_name_to_threshold_transformer = OrderedDict([
            (task_name, self.threshold_score_transformer_factory(
                name=task_name+"_label",
                track_name=task_name+"_contrib_scores"))
             for task_name in task_names]) 

        multitask_seqlet_creation_results = core.MultiTaskSeqletCreation(
            coord_producer=self.coord_producer,
            track_set=track_set,
            overlap_resolver=self.overlap_resolver)(
                task_name_to_score_track=per_position_contrib_scores,
                task_name_to_threshold_transformer=\
                    task_name_to_threshold_transformer)

        seqlets = multitask_seqlet_creation_results.final_seqlets

        attribute_vectors = (np.array([
                              [x[key+"_label"] for key in task_names]
                               for x in seqlets]))

        metaclustering_results = self.metaclusterer(attribute_vectors)
        metacluster_indices = metaclustering_results.metacluster_indices
        metacluster_idx_to_activity_pattern =\
            metaclustering_results.metacluster_idx_to_activity_pattern

        num_metaclusters = max(metacluster_indices)+1
        metacluster_sizes = [np.sum(metacluster_idx==metacluster_indices)
                              for metacluster_idx in range(num_metaclusters)]
        if (self.verbose):
            print("Metacluster sizes: ",metacluster_sizes)
            print("Idx to activities: ",metacluster_idx_to_activity_pattern)
            sys.stdout.flush()

        metacluster_idx_to_submetacluster_results = OrderedDict()

        for metacluster_idx, metacluster_size in\
            sorted(enumerate(metacluster_sizes), key=lambda x: x[1]):
            print("On metacluster "+str(metacluster_idx))
            print("Metacluster size", metacluster_size)
            sys.stdout.flush()
            metacluster_activities = [
                int(x) for x in
                metacluster_idx_to_activity_pattern[metacluster_idx].split(",")]
            assert len(seqlets)==len(metacluster_indices)
            metacluster_seqlets = [
                x[0] for x in zip(seqlets, metacluster_indices)
                if x[1]==metacluster_idx]
            relevant_task_names, relevant_task_signs =\
                zip(*[(x[0], x[1]) for x in
                    zip(task_names, metacluster_activities) if x[1] != 0])
            print('Relevant tasks: ', relevant_task_names)
            print('Relevant signs: ', relevant_task_signs)
            sys.stdout.flush()
            if (len(relevant_task_names) == 0):
                print("No tasks found relevant; skipping")
                sys.stdout.flush()
                continue
            
            seqlets_to_patterns = self.seqlets_to_patterns_factory(
                track_set=track_set,
                onehot_track_name="sequence",
                contrib_scores_track_names =\
                    [key+"_contrib_scores" for key in relevant_task_names],
                hypothetical_contribs_track_names=\
                    [key+"_hypothetical_contribs" for key in relevant_task_names],
                track_signs=relevant_task_signs,
                other_comparison_track_names=[])

            seqlets_to_patterns_result = seqlets_to_patterns(metacluster_seqlets)
            metacluster_idx_to_submetacluster_results[metacluster_idx] =\
                SubMetaclusterResults(
                    metacluster_size=metacluster_size,
                    activity_pattern=np.array(metacluster_activities),
                    seqlets=metacluster_seqlets,
                    seqlets_to_patterns_result=seqlets_to_patterns_result)

        return TfModiscoResults(
                 task_names=task_names,
                 multitask_seqlet_creation_results=
                    multitask_seqlet_creation_results,
                 seqlet_attribute_vectors=attribute_vectors,
                 metaclustering_results=metaclustering_results,
                 metacluster_idx_to_submetacluster_results=
                    metacluster_idx_to_submetacluster_results)

