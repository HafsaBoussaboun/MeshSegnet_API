# imporrter les librairies nécessaires
import io
from flask import Flask, request, jsonify, send_file
import os
import numpy as np
import torch
import torch.nn as nn
from meshsegnet import *
import vedo
import pandas as pd
from losses_and_metrics_for_mesh import *
from scipy.spatial import distance_matrix
import scipy.io as sio
import shutil
import time
from sklearn.svm import SVC
from sklearn.neighbors import KNeighborsClassifier
from pygco import cut_from_graph
import utils
from flask_cors import CORS  # Importez le module CORS

app = Flask(__name__)
# autoriser les requêtes depuis d'autres domaines à accéder à  l'application Flask
CORS(app)
@app.route('/hello', methods=['GET'])
def hello():
    return "Hello"

@app.route('/predict', methods=['POST'])
def predict():
    if 'meshFile' not in request.files:
        response = {'error': 'Aucun fichier n\'a été envoyé'}
        return jsonify(response), 400
    # Récupérer le fichier de l'objet 3D envoyé
    uploaded_file = request.files['meshFile']
    if uploaded_file.filename != '':
    # Enregistrer le fichier temporairement
        uploaded_file.save('temp_mesh.vtp')
    
    # upsampling_method = 'SVM'
    upsampling_method = 'KNN'

    model_path = './models' #need to modify with your path
    model_name = 'MeshSegNet_Max_15_classes_72samples_lr1e-2_best.zip' #need to modify with your model name
    mesh_path = './'  
    sample_filenames = ['temp_mesh.vtp'] # need to modify with your filename
    output_path = './outputs'
    if not os.path.exists(output_path):
        os.mkdir(output_path)



    num_classes = 15
    num_channels = 15

    # set model
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = MeshSegNet(num_classes=num_classes, num_channels=num_channels).to(device, dtype=torch.float)

    # load trained model
    checkpoint = torch.load(os.path.join(model_path, model_name), map_location='cpu')
    model.load_state_dict(checkpoint['model_state_dict'])
    del checkpoint
    model = model.to(device, dtype=torch.float)

    # Configuration de cuDNN pour accélérer les calculs GPU
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.enabled = True


    # Predicting
    model.eval()
    with torch.no_grad():
        for i_sample in sample_filenames:

            start_time = time.time()
            # create tmp folder
            tmp_path = './.tmp/'
            if not os.path.exists(tmp_path):
                os.makedirs(tmp_path)

            print('Predicting Sample filename: {}'.format(i_sample))
            # read image and label (annotation)
            mesh = vedo.load(os.path.join(mesh_path, i_sample))

            # pre-processing: downsampling
            print('\tDownsampling...')
            target_num = 10000
            ratio = target_num/mesh.ncells # calculate ratio
            mesh_d = mesh.clone()
            mesh_d.decimate(fraction=ratio)
            predicted_labels_d = np.zeros([mesh_d.ncells, 1], dtype=np.int32)

            # move mesh to origin
            print('\tPredicting...')
            points = mesh_d.points()
            mean_cell_centers = mesh_d.center_of_mass()
            points[:, 0:3] -= mean_cell_centers[0:3]

            ids = np.array(mesh_d.faces())
            cells = points[ids].reshape(mesh_d.ncells, 9).astype(dtype='float32')

            # customized normal calculation; the vtk/vedo build-in function will change number of points
            mesh_d.compute_normals()
            normals = mesh_d.celldata['Normals']

            # move mesh to origin
            barycenters = mesh_d.cell_centers() # don't need to copy
            barycenters -= mean_cell_centers[0:3]

            #normalized data
            maxs = points.max(axis=0)
            mins = points.min(axis=0)
            means = points.mean(axis=0)
            stds = points.std(axis=0)
            nmeans = normals.mean(axis=0)
            nstds = normals.std(axis=0)

            for i in range(3):
                cells[:, i] = (cells[:, i] - means[i]) / stds[i] #point 1
                cells[:, i+3] = (cells[:, i+3] - means[i]) / stds[i] #point 2
                cells[:, i+6] = (cells[:, i+6] - means[i]) / stds[i] #point 3
                barycenters[:,i] = (barycenters[:,i] - mins[i]) / (maxs[i]-mins[i])
                normals[:,i] = (normals[:,i] - nmeans[i]) / nstds[i]

            X = np.column_stack((cells, barycenters, normals))

            # computing A_S and A_L
            A_S = np.zeros([X.shape[0], X.shape[0]], dtype='float32')
            A_L = np.zeros([X.shape[0], X.shape[0]], dtype='float32')
            D = distance_matrix(X[:, 9:12], X[:, 9:12])
            A_S[D<0.1] = 1.0
            A_S = A_S / np.dot(np.sum(A_S, axis=1, keepdims=True), np.ones((1, X.shape[0])))

            A_L[D<0.2] = 1.0
            A_L = A_L / np.dot(np.sum(A_L, axis=1, keepdims=True), np.ones((1, X.shape[0])))

            # numpy -> torch.tensor
            X = X.transpose(1, 0)
            X = X.reshape([1, X.shape[0], X.shape[1]])
            X = torch.from_numpy(X).to(device, dtype=torch.float)
            A_S = A_S.reshape([1, A_S.shape[0], A_S.shape[1]])
            A_L = A_L.reshape([1, A_L.shape[0], A_L.shape[1]])
            A_S = torch.from_numpy(A_S).to(device, dtype=torch.float)
            A_L = torch.from_numpy(A_L).to(device, dtype=torch.float)

            tensor_prob_output = model(X, A_S, A_L).to(device, dtype=torch.float)
            patch_prob_output = tensor_prob_output.cpu().numpy()

            for i_label in range(num_classes):
                predicted_labels_d[np.argmax(patch_prob_output[0, :], axis=-1)==i_label] = i_label

            # output downsampled predicted labels
            mesh2 = mesh_d.clone()
            mesh2.celldata['Label'] = predicted_labels_d
            vedo.write(mesh2, os.path.join(output_path, '{}_d_predicted.vtp'.format(i_sample[:-4])))

            # refinement
            print('\tRefining by pygco...')
            round_factor = 100
            patch_prob_output[patch_prob_output<1.0e-6] = 1.0e-6

            # unaries
            unaries = -round_factor * np.log10(patch_prob_output)
            unaries = unaries.astype(np.int32)
            unaries = unaries.reshape(-1, num_classes)

            # parawise
            pairwise = (1 - np.eye(num_classes, dtype=np.int32))

            #edges
            normals = mesh_d.celldata['Normals'].copy() # need to copy, they use the same memory address
            barycenters = mesh_d.cell_centers() # don't need to copy
            cell_ids = np.asarray(mesh_d.faces())

            lambda_c = 30
            edges = np.empty([1, 3], order='C')
            for i_node in range(cells.shape[0]):
                # Find neighbors
                nei = np.sum(np.isin(cell_ids, cell_ids[i_node, :]), axis=1)
                nei_id = np.where(nei==2)
                for i_nei in nei_id[0][:]:
                    if i_node < i_nei:
                        cos_theta = np.dot(normals[i_node, 0:3], normals[i_nei, 0:3])/np.linalg.norm(normals[i_node, 0:3])/np.linalg.norm(normals[i_nei, 0:3])
                        if cos_theta >= 1.0:
                            cos_theta = 0.9999
                        theta = np.arccos(cos_theta)
                        phi = np.linalg.norm(barycenters[i_node, :] - barycenters[i_nei, :])
                        if theta > np.pi/2.0:
                            edges = np.concatenate((edges, np.array([i_node, i_nei, -np.log10(theta/np.pi)*phi]).reshape(1, 3)), axis=0)
                        else:
                            beta = 1 + np.linalg.norm(np.dot(normals[i_node, 0:3], normals[i_nei, 0:3]))
                            edges = np.concatenate((edges, np.array([i_node, i_nei, -beta*np.log10(theta/np.pi)*phi]).reshape(1, 3)), axis=0)
            edges = np.delete(edges, 0, 0)
            edges[:, 2] *= lambda_c*round_factor
            edges = edges.astype(np.int32)

            refine_labels = cut_from_graph(edges, unaries, pairwise)
            refine_labels = refine_labels.reshape([-1, 1])

            # output refined result
            mesh3 = mesh_d.clone()
            mesh3.celldata['Label'] = refine_labels
            vedo.write(mesh3, os.path.join(output_path, '{}_d_predicted_refined.vtp'.format(i_sample[:-4])))

            # upsampling
            print('\tUpsampling...')
            if mesh.ncells > 50000:
                target_num = 50000 # set max number of cells
                ratio = target_num/mesh.ncells # calculate ratio
                mesh.decimate(fraction=ratio)
                print('Original contains too many cells, simpify to {} cells'.format(mesh.ncells))

            # get fine_cells
            barycenters = mesh3.cell_centers() # don't need to copy
            fine_barycenters = mesh.cell_centers() # don't need to copy

            if upsampling_method == 'SVM':
                clf = SVC(kernel='rbf', gamma='auto')
                # train SVM
                clf.fit(barycenters, np.ravel(refine_labels))
                fine_labels = clf.predict(fine_barycenters)
                fine_labels = fine_labels.reshape(-1, 1)
            elif upsampling_method == 'KNN':
                neigh = KNeighborsClassifier(n_neighbors=3)
                # train KNN
                neigh.fit(barycenters, np.ravel(refine_labels))
                fine_labels = neigh.predict(fine_barycenters)
                fine_labels = fine_labels.reshape(-1, 1)

            mesh.celldata['Label'] = fine_labels

            
            vedo.write(mesh, os.path.join(output_path, '{}_predicted_refined.vtp'.format(i_sample[:-4])))

            #remove tmp folder
            shutil.rmtree(tmp_path)

            end_time = time.time()
            print('Sample filename: {} completed'.format(i_sample))
            print('\tcomputing time: {0:.2f} sec'.format(end_time-start_time))
        os.remove('temp_mesh.vtp')


        # Reading the predicted_refined.vtp file
        predicted_refined_path = os.path.join(output_path, '{}_predicted_refined.vtp'.format(i_sample[:-4]))
        with open(predicted_refined_path, 'rb') as f:
            predicted_refined_data = f.read()

        # Send the predicted_refined.vtp file as a response
        return send_file(
        io.BytesIO(predicted_refined_data),  # Create a bytes buffer
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name='predicted_refined.vtp'  # Specify the download name
    )
        

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000,debug=True)
