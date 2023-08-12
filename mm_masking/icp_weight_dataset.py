import cv2
import torch
import numpy as np
import os.path as osp
import os
from pyboreas.utils.odometry import read_traj_file2, read_traj_file_gt2
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
from pylgmath import se3op, Transformation
from radar_utils import load_radar, cfar_mask, extract_pc, load_pc_from_file, radar_cartesian_to_polar, radar_polar_to_cartesian_diff
from dICP.ICP import ICP
from pyboreas.utils.utils import (
    SE3Tose3,
    get_closest_index,
    get_inverse_tf,
    rotToRollPitchYaw,
)
import vtr_pose_graph
from vtr_pose_graph.graph_factory import Rosbag2GraphFactory
import vtr_pose_graph.graph_utils as g_utils
from vtr_pose_graph.graph_iterators import TemporalIterator
from utils.extract_graph import extract_points_and_map
import time

class ICPWeightDataset():

    def __init__(self, gt_data_dir, pc_dir, radar_dir, loc_pairs,
                 map_sensor='lidar', loc_sensor='radar',
                 random=False, num_samples=-1, size_pc=-1, verbose=False,
                 float_type=torch.float64, use_gt=False, gt_eye=True, pos_std=1.0, rot_std=0.1,
                 a_thresh=1.0, b_thresh=0.09):
        self.loc_pairs = loc_pairs
        self.size_pc = size_pc
        self.float_type = float_type

        if not random:
            np.random.seed(99)
            torch.manual_seed(99)

        # Loop through loc_pairs and load in ground truth loc poses
        T_loc_gt = None
        T_loc_init = None
        map_pc_list = []
        self.loc_timestamps = []
        self.map_timestamps = []

        dataset_dir = '../data/vtr_data/boreas'
        vtr_result_dir = '../data/vtr_results'

        
        for pair in loc_pairs:
            map_seq = pair[0]
            loc_seq = pair[1]            
            
            # Load in ground truth localization to map poses
            # T_gt is the ground truth localization transform between the current scan
            # and the reference submap scan
            gt_file = osp.join(gt_data_dir, map_seq, loc_seq + '.txt')
            T_gt, loc_times, map_times, _, _ = read_traj_file2(gt_file)

            gt_map_poses, gt_map_times = read_traj_file_gt2(osp.join(dataset_dir, map_seq, "applanix", map_sensor + "_poses.csv"), dim=2)
            gt_loc_poses, gt_loc_times = read_traj_file_gt2(osp.join(dataset_dir, loc_seq, "applanix", loc_sensor + "_poses.csv"), dim=2)








            # Assemble paths
            if map_sensor == 'lidar' and loc_sensor == 'radar':
                sensor_dir_name = 'radar_lidar'
                msg_name = 'radar_raw_point_cloud'
            elif map_sensor == 'radar' and loc_sensor == 'radar':
                sensor_dir_name = 'radar'
                msg_name = 'raw_point_cloud'
            else:
                raise ValueError("Invalid sensor combination")
            graph_dir = osp.join(vtr_result_dir, sensor_dir_name, map_seq, loc_seq, 'graph')

            factory = Rosbag2GraphFactory(graph_dir)

            test_graph = factory.buildGraph()
            print(f"Graph {test_graph} has {test_graph.number_of_vertices} vertices and {test_graph.number_of_edges} edges")

            g_utils.set_world_frame(test_graph, test_graph.root)
            v_start = test_graph.get_vertex((1,0))
            loc_gt_list = []
            v_id_list = []
            ii = -1
            for vertex, e in TemporalIterator(v_start):
                ii += 1
                if e.from_id == vtr_pose_graph.INVALID_ID:
                    continue
                curr_pts, curr_norms, map_pts, maps_norms, T_w_v_curr, T_w_v_map = extract_points_and_map(test_graph, vertex, msg=msg_name)

                loc_stamp = int(vertex.stamp * 1e-3)
                teach_v = g_utils.get_closest_teach_vertex(vertex)
                map_ptr = teach_v.get_data("pointmap_ptr")
                teach_v = test_graph.get_vertex(map_ptr.map_vid)
                map_stamp = int(teach_v.stamp * 1e-3)

                # Ensure timestamps match gt
                assert loc_stamp == gt_loc_times[ii], "query: {}".format(loc_stamp)
                closest_map_t = get_closest_index(map_stamp, gt_map_times)
                assert map_stamp == gt_map_times[closest_map_t], "query: {}".format(map_stamp)
                gt_map_pose_idx = gt_map_poses[closest_map_t]
                gt_T_s1_s2 = get_inverse_tf(gt_map_pose_idx) @ gt_loc_poses[ii]
                gt_T_s2_s1 = get_inverse_tf(gt_T_s1_s2)

                curr_pts_map_frame = (gt_T_s1_s2[:3, :3] @ curr_pts.T + gt_T_s1_s2[:3, 3:4]).T

                yfwd2xfwd = np.array([[0, 1, 0, 0], [-1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
                T_applanix_lidar = np.array([[7.360555651493132512e-01, -6.769211217083995757e-01, 0, 0], [6.769211217083995757e-01, 7.360555651493132512e-01, 0, 0], [0, 0, 1, 1.300000000000000044e-01], [0, 0, 0, 1]])
                T_radar_lidar = np.array([[6.823393919112460404e-01, 7.310355355563714630e-01, 0, 0],
                [7.310355355563714630e-01, -6.823393919112460404e-01, 0, 0],
                [0, 0, -1, 3.649999999999999911e-01],
                [0, 0, 0, 1]])
                if map_sensor == 'radar':
                    T_robot_map_sensor = yfwd2xfwd @ T_applanix_lidar @ get_inverse_tf(T_radar_lidar)
                elif map_sensor == 'lidar':
                    T_robot_map_sensor = yfwd2xfwd @ T_applanix_lidar
                T_map_sensor_robot = get_inverse_tf(T_robot_map_sensor)
                map_pts = (T_map_sensor_robot[:3,:3] @ map_pts.T + T_map_sensor_robot[:3, 3:4]).T
                #map_pts[:, 1] = -map_pts[:, 1]

                #map_pts = (yfwd2xfwd[:3,:3] @ map_pts.T).T
                map_pts_curr_frame = (gt_T_s2_s1[:3, :3] @ map_pts.T + gt_T_s2_s1[:3, 3:4]).T
                print(gt_T_s1_s2)

                loc_radar_path = osp.join(radar_dir, loc_seq, str(loc_stamp) + '.png')
                loc_radar_img = cv2.imread(loc_radar_path, cv2.IMREAD_GRAYSCALE)
                loc_radar_mat = np.asarray(loc_radar_img)
                fft_data, azimuths, az_timestamps = load_radar(loc_radar_mat)
                fft_data = torch.tensor(fft_data, dtype=self.float_type).unsqueeze(0)
                azimuths = torch.tensor(azimuths, dtype=self.float_type).unsqueeze(0)
                az_timestamps = torch.tensor(az_timestamps, dtype=self.float_type).unsqueeze(0)
                fft_cfar = cfar_mask(fft_data, 0.0596, a_thresh=1.0, b_thresh=0.09, diff=False)

                # Extract pointcloud from fft data
                if gt_eye:
                    T_scan_pc = T_loc_gt
                else:
                    T_scan_pc = None
                scan_pc_list = extract_pc(fft_cfar, 0.0596, azimuths, az_timestamps,
                                        T_ab=None, diff=False)
                scan_pc = scan_pc_list[0].numpy()
                
                plt.figure(figsize=(15,15))
                plt.scatter(map_pts_curr_frame[:,0], map_pts_curr_frame[:,1], s=1.0, c='red')
                #plt.scatter(map_pts[:,0], map_pts[:,1], s=1.0, c='red')
                plt.scatter(curr_pts[:,0], curr_pts[:,1], s=0.5, c='blue')
                # plt.scatter(scan_pc[:,0], scan_pc[:,1], s=0.5, c='green')
                #plt.scatter(curr_pts_map_frame[:,0], curr_pts_map_frame[:,1], s=0.5, c='green')
                plt.ylim([-80, 80])
                plt.xlim([-80, 80])
                plt.savefig('align.png')
                time.sleep(0.1)
                plt.close()
                if ii > 200:
                    dsfdfsa











            # Find localization cartesian images with names corresponding to pred_times
            loc_timestamps = []
            map_timestamps = []
            all_fft_data = None
            all_azimuths = None
            all_az_timestamps = None
            incomplete_loc_times = []
            for idx, loc_time in enumerate(loc_times):
                # Load in localization paths
                loc_radar_path = osp.join(radar_dir, loc_seq, str(loc_time) + '.png')
                #loc_pc_path = osp.join(pc_dir, sensor, loc_seq, str(loc_time) + '.bin')
                loc_pc_path = osp.join(pc_dir, loc_seq, str(loc_time) + '.bin')

                # Load in map paths
                #map_cart_path = osp.join(cart_dir, map_seq, str(map_times[idx]) + '.png')
                map_pc_path = osp.join(pc_dir, map_sensor, map_seq, str(map_times[idx]) + '.bin')
                #map_pc_path = osp.join(pc_dir, map_seq, str(map_times[idx]) + '.bin')

                # Check if the paths exist, if not then save timestamps that don't exist
                if not osp.exists(loc_radar_path) or not osp.exists(loc_pc_path) or \
                    not osp.exists(map_pc_path):
                    if verbose:
                        print('WARNING: Images or point clouds don\'t exist')
                        print('Localization time: ' + str(loc_time))
                        print('Map time: ' + str(map_times[idx]))

                    # Save timestamp that does not exist
                    incomplete_loc_times.append(loc_time)
                    continue
                else:
                    # Save timestamp
                    loc_timestamps.append(loc_time)
                    map_timestamps.append(map_times[idx])

                    # Save fft data
                    # Load in localization polar image
                    loc_radar_img = cv2.imread(loc_radar_path, cv2.IMREAD_GRAYSCALE)
                    loc_radar_mat = np.asarray(loc_radar_img)
                    fft_data, azimuths, az_timestamps = load_radar(loc_radar_mat)
                    fft_data = torch.tensor(fft_data, dtype=self.float_type).unsqueeze(0)
                    azimuths = torch.tensor(azimuths, dtype=self.float_type).unsqueeze(0)
                    az_timestamps = torch.tensor(az_timestamps, dtype=self.float_type).unsqueeze(0)

                    if all_fft_data is None:
                        all_fft_data = fft_data
                        all_azimuths = azimuths
                        all_az_timestamps = az_timestamps
                    else:
                        all_fft_data = torch.cat((all_fft_data, fft_data), dim=0)
                        all_azimuths = torch.cat((all_azimuths, azimuths), dim=0)
                        all_az_timestamps = torch.cat((all_az_timestamps, az_timestamps), dim=0)

                    # Save ground truth localization to map pose
                    T_gt_idx = torch.tensor(T_gt[idx], dtype=float_type)
                    # Also, generate random perturbation to ground truth pose
                    # The map pointcloud is transformed into the scan frame using T_gt
                    # T_init is the initial guess that is offset from T_gt that the ICP
                    # needs to "unlearn" to get to identity
                    if use_gt:
                        if gt_eye:
                            T_init_idx = np.eye(4)
                        else:
                            T_init_idx = T_gt[idx]
                    else:
                        xi_rand = torch.rand((6,1), dtype=float_type)
                        # Zero out z, pitch, and roll
                        xi_rand[2:5] = 0.0
                        # Scale x and y
                        xi_rand[0:2] = pos_std*xi_rand[0:2]
                        # Scale yaw
                        xi_rand[5] = rot_std*xi_rand[5]
                        T_rand = Transformation(xi_ab=xi_rand)
                        if gt_eye:
                            T_init_idx = T_rand.matrix() # @ identity
                        else:
                            T_init_idx = T_rand.matrix() @ T_gt[idx]
                    T_init_idx = torch.tensor(T_init_idx, dtype=float_type)

                    # Stack transformations
                    if T_loc_gt is None:
                        T_loc_gt = T_gt_idx.unsqueeze(0)
                        T_loc_init = T_init_idx.unsqueeze(0)
                    else:
                        T_loc_gt = torch.cat((T_loc_gt, T_gt_idx.unsqueeze(0)), dim=0)
                        T_loc_init = torch.cat((T_loc_init, T_init_idx.unsqueeze(0)), dim=0)
                
                    # Load in map pointcloud
                    map_pc_ii = load_pc_from_file(map_pc_path, to_type=self.float_type, flip_y=True)
                    # If want groundtruth to be identity, transform map point cloud to scan frame
                    if gt_eye:
                        T_sm = torch.linalg.inv(T_gt_idx)
                        # Transform points
                        map_pc_ii[:, :3] = (T_sm[:3, :3] @ map_pc_ii[:, :3].T).T + T_sm[:3, 3]
                        # Transform normals
                        n_hg = torch.cat((map_pc_ii[:, 3:], torch.ones((map_pc_ii.shape[0], 1), dtype=self.float_type)), dim=1)
                        n_hg = (torch.linalg.inv(T_sm).T @ n_hg.T).T
                        map_pc_ii[:, 3:] = n_hg[:, :3]

                    map_pc_list.append(map_pc_ii)


                if num_samples > 0 and T_loc_gt.shape[0] >= num_samples:
                    break
            # Remove ground truth localization to map poses that do not have corresponding images
            if len(incomplete_loc_times) != 0:
                print('WARNING: Number of localization cartesian images does not match number of localization poses')
                # Remove ground truth localization to map poses that do not have corresponding images
                for time_idx in incomplete_loc_times:
                    index = loc_times.index(time_idx)
                    loc_times.pop(index)
                    map_times.pop(index)
                    T_gt.pop(index)

            #self.T_loc_init += T_gt_used
            self.loc_timestamps += loc_timestamps
            self.map_timestamps += map_timestamps

        self.fft_data = all_fft_data
        self.azimuths = all_azimuths
        self.az_timestamps = all_az_timestamps


        # Want to bind the range of polar data to fit within cartesian image
        polar_res = 0.0596

        # Precompute CFAR of fft data
        self.fft_cfar = cfar_mask(all_fft_data, polar_res, a_thresh=a_thresh, b_thresh=b_thresh, diff=False)

        # Extract pointcloud from fft data
        if gt_eye:
            T_scan_pc = T_loc_gt
        else:
            T_scan_pc = None
        # Note, we've already transformed the map pointcloud to the scan frame
        scan_pc_list = extract_pc(self.fft_cfar, polar_res, all_azimuths, all_az_timestamps,
                                  T_ab=None, diff=False)

        # Form batch of pointclouds and initial guesses for batching
        config_path = '../external/dICP/config/dICP_config.yaml'
        temp_ICP = ICP(config_path=config_path)
        scan_pc_batch, map_pc_batch, _, _ = temp_ICP.batch_size_handling(scan_pc_list, map_pc_list)
        # Want to bind the range of polar data to fit within cartesian image
        cart_res = 0.2384
        cart_pixel_width = 640
        # Compute the range (m) captured by pixels in cartesian scan
        if (cart_pixel_width % 2) == 0:
            cart_min_range = (cart_pixel_width / 2 - 0.5) * cart_res
        else:
            cart_min_range = cart_pixel_width / 2 * cart_res
        scan_pc_x_outrange = torch.abs(scan_pc_batch[:, :, 0]) > cart_min_range
        scan_pc_y_outrange = torch.abs(scan_pc_batch[:, :, 1]) > cart_min_range
        scan_pc_outrange = scan_pc_x_outrange | scan_pc_y_outrange
        scan_pc_batch[scan_pc_outrange] = 0.0

        self.scan_pc = scan_pc_batch
        self.map_pc = map_pc_batch
        self.T_loc_init = T_loc_init
        self.T_loc_gt = T_loc_gt

        # Save fft data statistics for potential normalization
        self.fft_mean = torch.mean(self.fft_data)
        self.fft_std = torch.std(self.fft_data)
        self.fft_max = torch.max(self.fft_data)
        self.fft_min = torch.min(self.fft_data)

        # Assert that the number of all lists are the same
        assert len(self.T_loc_gt) \
             == self.fft_data.shape[0] == self.azimuths.shape[0] \
             == self.az_timestamps.shape[0] == len(self.loc_timestamps)

    def __len__(self):
        return self.T_loc_gt.shape[0]

    def __getitem__(self, index):
        # Load in fft data
        fft_data = self.fft_data[index]
        azimuths = self.azimuths[index]
        az_timestamps = self.az_timestamps[index]
        fft_cfar = self.fft_cfar[index]
        scan_pc = self.scan_pc[index]
        map_pc = self.map_pc[index]
        T_init = self.T_loc_init[index]

        # Load in timestamps
        loc_timestamp = self.loc_timestamps[index]
        map_timestamp = self.map_timestamps[index]

        # Load in ground truth localization to map pose
        T_ml_gt = self.T_loc_gt[index]

        #loc_data = {'pc' : loc_pc, 'timestamp' : loc_timestamp}
        loc_data = {'pc': scan_pc, 'timestamp' : loc_timestamp,
                    'fft_data' : fft_data, 'azimuths' : azimuths, 'az_timestamps' : az_timestamps,
                    'fft_cfar' : fft_cfar}
        map_data = {'pc': map_pc, 'timestamp' : map_timestamp}
        T_data = {'T_ml_init' : T_init, 'T_ml_gt' : T_ml_gt}

        return {'loc_data': loc_data, 'map_data': map_data, 'transforms': T_data}
    