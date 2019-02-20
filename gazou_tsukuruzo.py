# 物体検出スクリプトをテストするための画像を作る
# アノテーションファイルも作る
import cv2
import numpy as np
import random

w = 640
h = 480
N = 1000
tsize = 40
classesN = 0

fout = open("annotation.txt", "w") # txtファイルを書き込みモードで作成

for i in range(N):
    img = np.ones((h,w,3), np.uint8)
    img = img * 255

    for c in range(100):
        hr = random.randint(0, h)
        wr = random.randint(0, w)
        radr = random.randint(0, 100)
        rr = random.randint(0, 255)
        gr = random.randint(0, 255)
        br = random.randint(0, 255)
        img = cv2.circle(img,(wr,hr), radr, (rr,gr,br), -1)

    hr = random.randint(0, h-tsize)
    wr = random.randint(0, w-tsize)

    pts = np.array([[wr+tsize/2,hr],[wr,hr+tsize],[wr+tsize,hr+tsize]], np.int32)
    pts = pts.reshape((-1,1,2))
    #img = cv2.polylines(img,[pts],True,(0,0,0))
    img = cv2.fillConvexPoly(img, pts, (0,0,0))

    #画像を保存
    imname = 'JPG/' + 'img' + str(i) + '.jpg'
    cv2.imwrite(imname, img)
    #アノテーションファイルに記録
    annostr = 'model_data/' + imname + ' ' + str(wr) + ','  + str(hr) + ','  + str(wr+tsize) + ',' + str(hr+tsize) + ',' + str(classesN)
    fout.writelines(annostr + "\n")

fout.close()
